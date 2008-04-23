import datetime
import logging
import pickle
import re
import time

from google.appengine.ext import db
from google.appengine.ext import search

import accounts
import system

class QuoteException(Exception):
  pass

class InvalidQuoteStateException(QuoteException): pass
class InvalidKeyException(QuoteException): pass
class NoPermissionException(QuoteException): pass


VERB_PUBLISHED = system.Verb('published')
VERB_DELETED = system.Verb('deleted')
VERB_UPDATED = system.Verb('updated')

@system.capture(VERB_PUBLISHED)
def onQuotePublished(action):
  system.incrementQuoteCount()


@system.capture(VERB_DELETED)
def onQuoteDeleted(action):
  system.incrementQuoteCount(-1)


class Line:
  def __init__(self, line, preserve_formatting=False):
    self.original = line
    if preserve_formatting:
      self.formatting = []
    else:
      self.formatting = list(LineFormatterRegistry.parse(line))

  def __repr__(self):
    if len(self.original) > 20:
      line = self.original[:17] + '...'
    else:
      line = self.original
    return '<Line: %r formatting=%s>' % (line, self.formatting)


class LineFormatterRegistry(type):
  NL = re.compile(r'\r?\n')
  INDENT = re.compile(r'^(\s*)')

  registry = []
  
  def __new__(cls, *args, **kwargs):
    instance = type.__new__(cls, *args, **kwargs)
    cls.registry.append(instance)
    return instance

  @classmethod
  def parseDialog(cls, dialog, preserve_formatting):
    dialog = dialog.strip()
    line_start_indent = 0
    cur_line = []
    for line in cls.NL.split(dialog):
      indent = len(cls.INDENT.match(line).group(1))
      if indent <= line_start_indent:
        if cur_line:
          yield Line(' '.join(cur_line),
                     preserve_formatting=preserve_formatting)
        del cur_line[:]
        line_start_indent = indent
      cur_line.append(line.strip())
    if cur_line:
      yield Line(' '.join(cur_line),
                 preserve_formatting=preserve_formatting)

  @classmethod
  def parse(cls, line):
    for formatter in cls.registry:
      while True:
        match = formatter.match(line)
        if match:
          yield match
          line = line[:match.range[0]] + line[match.range[1]:]
          if not match.multiple:
            break
        else:
          break


class LineFormatter(object):
  """Instances of this class describe how to apply formatting to a quote.

  @type multiple: bool
  @ivar multiple: Whether the formatter could possibly match again.
  @type range: (int, int)
  @ivar range: A tuple giving the range of characters this formatting effect
               applies to.  E.g., line[range[0]:range[1]].
  @type params: A dictionary of data to export to the recipient of the formatted
                line.
  """

  __metaclass__ = LineFormatterRegistry

  def __init__(self, range=None, params=None, multiple=False):
    self.range = range
    self.params = params
    self.multiple = multiple

  def __repr__(self):
    #return '%s(%r, %r)' % (self.__class__.__name__, self.range, self.params)
    return '%s: %r' % (self.__class__.__name__, self.__dict__)

  @classmethod
  def match(cls, line):
    return None


class TimestampFormatter(LineFormatter):
  TIME = re.compile(r'^\s*[\[(]?(?P<hour>\d?\d):(?P<minute>\d\d)(:(?P<second>\d\d))?[)\]]?\s*')

  @classmethod
  def match(cls, line):
    match = cls.TIME.match(line)
    if match:
      groups = match.groupdict(0)
      timestamp = datetime.time(int(groups['hour']), int(groups['minute']),
                                int(groups['second']))
      return cls(range=(match.start(), match.end()),
                 params={'timestamp': timestamp},
                )


class NickFormatter(LineFormatter):
  NICK = re.compile(r'^\s*[\[<\(]?'
                    r'(?P<nickflag>[\s@+])?'
                    r"(?P<nick>[\w\d`\[\]{}\\|-]+)[\]>\):]+\s?")
  NORMALIZATION = re.compile('[^\w\d]')

  @classmethod
  def match(cls, line):
    match = cls.NICK.match(line)
    if match and filter(lambda c: not c.isdigit(), match.group('nick')):
      params = {
        'normalized_nick':
          cls.NORMALIZATION.sub('', match.group('nick')).lower(),
      }
      params.update(match.groupdict())
      return cls(range=(match.start(), match.end()),
                 params=params,
                )


class Quote(search.SearchableModel):
  # The text data
  dialog_source = db.TextProperty(required=True)
  formatting = db.BlobProperty()
  preserve_formatting = db.BooleanProperty(default=False)
  note = db.TextProperty()
  labels = db.StringListProperty()
  location_labels = db.StringListProperty()

  # State bits
  deleted = db.BooleanProperty(default=False)
  draft = db.BooleanProperty(required=True, default=True)

  # Timestamps
  submitted = db.DateTimeProperty(required=True, auto_now_add=True)
  modified = db.DateTimeProperty()
  built = db.DateTimeProperty(default=datetime.datetime.fromtimestamp(0))

  # Migration support
  legacy_id = db.IntegerProperty()

  # Editing support
  clone_of = db.SelfReferenceProperty()

  @classmethod
  def createDraft(cls, account, source,
                  context=None,
                  note=None,
                  submitted=None,
                  legacy_id=None,
                 ):
    logging.info('creating draft by %r', account)
    kwargs = {}
    if submitted:
      kwargs['submitted'] = submitted
    quote = cls(parent=account,
                draft=True,
                context=context,
                dialog_source=source,
                note=note,
                legacy_id=legacy_id,
                **kwargs
               )
    quote.rebuild()
    def transaction():
      acc = accounts.Account.get(account.key())
      acc.draft_count += 1
      acc.put()
      return quote
    return db.run_in_transaction(transaction)

  @classmethod
  def createLegacy(cls, quote_id, account, network, server, channel, source,
                   note, modified, submitted):
    loc_labels = []
    def labelize(type, value):
      value = value.strip().replace(' ', '-').replace('#', '')
      if value:
        loc_labels.append('%s:%s' % (type, value))
    labelize('network', network)
    labelize('server', server)
    labelize('channel', channel)

    quote = cls.getByLegacyId(quote_id)
    if quote:
      new = False
      quote.dialog_source = source
      quote.note = note
      quote.submitted = submitted
      quote.modified = modified or submitted
    else:
      new = True
      quote = cls(parent=account,
                  legacy_id=quote_id,
                  dialog_source=source,
                  note=note,
                  submitted=submitted,
                  modified=modified or submitted,
                  draft=False,
                 )
    quote.rebuild()
    if new:
      system.record(account, VERB_PUBLISHED, quote, timestamp=submitted)
    return quote

  @classmethod
  def getDraft(cls, account, key):
    draft = cls.getQuoteByKey(account, key)
    if not draft or draft.deleted:
      raise InvalidKeyException
    if not draft.draft:
      raise InvalidQuoteStateException
    return draft

  @classmethod
  def getQuoteByKey(cls, account, key):
    quote = cls.get(key)
    if not quote or quote.deleted:
      raise InvalidKeyException
    if quote.draft and account.key() != quote.parent_key():
      raise NoPermissionException
    return quote

  @classmethod
  def getByLegacyId(cls, legacy_id):
    query = cls.all()
    query.filter('legacy_id =', legacy_id)
    query.filter('deleted =', False)
    return query.get()

  @classmethod
  def getPublishedQuote(key):
    quote = cls.get(key)
    if quote and not quote.deleted and not quote.draft:
      return quote

  @classmethod
  def getRecentQuotes(cls, reversed=False, **kwargs):
    return cls.getQuotesByTimestamp('submitted',
                                    descending=not reversed,
                                    include_drafts=False,
                                    **kwargs)

  @classmethod
  def getQuotesByBuildTime(cls, **kwargs):
    return cls.getQuotesByTimestamp('built', **kwargs)

  @classmethod
  def getQuotesByTimestamp(cls, property,
                           start=None,
                           offset=0,
                           limit=10,
                           descending=False,
                           include_drafts=True,
                           ancestor=None,
                          ):
    logging.info('quotes by ts: property=%s, start=%s, offset=%s limit=%s, descending=%s, drafts=%s, ancestor=%s',
                 property, start, offset, limit, descending, include_drafts, ancestor)
    query = cls.all()
    if ancestor:
      query.ancestor(ancestor)
    if not include_drafts:
      query.filter('draft =', False)
    query.filter('deleted =', False)
    op = '>='
    if descending:
      op = '<='
    if start is not None:
      logging.info('%s %s %s', property, op, start)
      query.filter('%s %s' % (property, op), start)
    if descending:
      query.order('-%s' % property)
    else:
      query.order(property)
    logging.info('offset=%d, limit=%d', offset, limit)
    quotes = list(query.fetch(offset=offset, limit=limit))
    logging.info('got back %d quotes', len(quotes))
    logging.info('%s', [(i, str(quotes[i].submitted), quotes[i].submitted) for i in xrange(len(quotes))])
    if len(quotes) == limit:
      for i in xrange(2, limit + 1):
        if quotes[-i].submitted != quotes[-1].submitted:
          break
      start = quotes[-1].submitted
      offset = i - 1
    return quotes, start, offset

  @classmethod
  def getDraftQuotes(cls, account, offset=0, limit=10, order='-submitted'):
    query = (cls.all()
             .ancestor(account)
             .filter('draft =', True)
             .filter('deleted =', False)
             .order(order)
            )
    return list(query.fetch(offset=offset, limit=limit))

  @classmethod
  def search(cls, query, offset=0, limit=10):
    logging.info('quote search: query=%r, offset=%r, limit=%r', query, offset, limit)
    db_query = cls.all()
    db_query.search(query)
    db_query.filter('draft =', False)
    db_query.filter('deleted =', False)
    return list(db_query.fetch(offset=offset, limit=limit))

  def getProperties(self):
    return dict([(prop, getattr(self, prop, None))
                 for prop in self.properties()])

  def clone(self, target=None):
    if target is None:
      return Quote(parent=self.parent(), **self.getProperties())
    else:
      for name, value in self.getProperties().iteritems():
        setattr(target, name, value)
      return target

  def edit(self, account):
    if self.draft:
      raise InvalidQuoteStateException
    if account.key() != self.parent_key():
      raise NoPermissionException
    if self.clone_of:
      return self.clone_of
    draft = self.clone()
    draft.draft = True
    draft.put()
    self.clone_of = draft
    return draft

  def unpublish(self):
    self.deleted = True
    self.put()
    system.record(self.parent(), VERB_DELETED, self)

  def republish(self, modified=None):
    draft = self.clone_of
    draft.clone(self)
    draft.delete()
    self.clone_of = None
    self.publish(modified=modified)
    system.record(self.parent(), VERB_UPDATED, self)

  def publish(self, modified=None):
    if not self.draft:
      raise InvalidQuoteStateException
    if self.clone_of:
      self.clone_of.republish(self, modified=modified)
      return
    def transaction():
      self.draft = False
      self.modified = modified or datetime.datetime.now()
      self.put()
      account = accounts.Account.get(self.parent_key())
      account.quote_count += 1
      account.draft_count -= 1
      account.put()
    db.run_in_transaction(transaction)
    system.record(self.parent(), VERB_PUBLISHED, self)
    return self

  def update(self,
             dialog=None,
             preserve_formatting=None,
             modified=None,
             publish=False,
            ):
    if not self.draft:
      raise InvalidQuoteStateException
    if dialog is not None:
      self.dialog_source = dialog
    if preserve_formatting is not None:
      self.preserve_formatting = preserve_formatting
    self.rebuild()
    if publish:
      self.publish(modified=modified)
    else:
      system.record(self.parent(), VERB_UPDATED, self)

  def getDialog(self):
    return self.getFormattedDialog()

  def getFormattedDialog(self):
    lines = pickle.loads(self.formatting)
    logging.info('lines: %s', lines)
    for line in lines:
      params = {}
      text = line.original
      for formatter in line.formatting:
        text = text[:formatter.range[0]] + text[formatter.range[1]:]
        params.update(formatter.params)
      yield {'text': text, 'params': params}

  def rebuild(self):
    lines = list(LineFormatterRegistry.parseDialog(self.dialog_source,
                                                   preserve_formatting=
                                                     self.preserve_formatting))
    logging.info('formatting: %s', lines)
    self.formatting = db.Blob(pickle.dumps(lines))

    nicks = set()
    for line in lines:
      if line.formatting:
        for formatter in line.formatting:
          if 'normalized_nick' in formatter.params:
            nicks.add(formatter.params['normalized_nick'])
    for nick in nicks:
      self.addLabel('nick:%s' % nick)
    logging.info('labels: %r', self.labels)
    self.built = datetime.datetime.now()
    self.put()

  def label(self, prefix):
    return [value for value in self.labels if value.startswith(prefix)]

  def clearLabels(self):
    del self.labels[:]

  def addLabel(self, label):
    if self.labels is None:
      self.labels = []
    label = label.strip().lower()
    if label not in self.labels:
      logging.info('adding label: %r', label)
      self.labels.append(label)

  def formattedSubmitted(self):
    return self.formattedTimestamp(self.submitted)

  def formattedModified(self):
    return self.formattedTimestamp(self.modified)

  @staticmethod
  def formattedTimestamp(timestamp):
    return timestamp.strftime('%B %e, %Y, at %T %Z')
