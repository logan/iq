import datetime
import logging
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
VERB_UPDATED = system.Verb('updated')

@system.capture(VERB_PUBLISHED)
def onQuotePublished(action):
  system.incrementQuoteCount()


class Network(db.Model):
  name = db.StringProperty(required=True)
  canonical_name = db.StringProperty()
  servers = db.StringListProperty()

  def put(self):
    self.canonical_name = self.name.lower()
    return db.Model.put(self)

  @staticmethod
  def getOrCreate(network, server):
    server = server.lower()
    entity = Network.all().filter('canonical_name =', network.lower()).get()
    if entity:
      if server not in entity.servers:
        entity.servers.append(server)
        entity.put()
    else:
      entity = Network(name=network or server, servers=[server])
      entity.put()
    return entity


class Context(db.Model):
  protocol = db.StringProperty(required=True)
  network = db.ReferenceProperty(Network)
  location = db.StringProperty()

  @staticmethod
  def getIrc(network, server, channel):
    if network or server:
      network = Network.getOrCreate(network, server)
    else:
      network = None
    query = Context.all()
    query.filter('protocol =', 'irc')
    query.filter('network =', network)
    query.filter('location =', channel)
    context = query.get()
    if not context:
      context = Context(protocol='irc', network=network, location=channel)
      context.put()
    return context


class DialogLine(db.Model):
  offset = db.IntegerProperty(required=True)
  time = db.TimeProperty()
  actor = db.StringProperty()
  text = db.TextProperty(default='')

  NL = re.compile(r'\r?\n')
  INDENT = re.compile(r'^(\s*)')
  STATEMENT = re.compile(r'^(?P<timestamp>\d?\d:\d\d(:\d\d)?)?\s*'
                         r"(<?(?P<actor>\W*[\w\d'\[\]{}\\|]+)\W*)?\s"
                         r'(?P<statement>.*)'
                        )
  TIME = re.compile(r'(?P<hour>\d?\d):(?P<minute>\d\d)(:(?P<second>\d\d))?')
  NICK = re.compile(r"(?P<nick>[\w\d'\[\]{}\\|]+)")
  WORD_SPLITTER = re.compile(r'\s+')
  WORD_STRIPPER = re.compile(r'\W+')

  MAX_SIGNATURE_WORDS = 10
  MAX_SIGNATURE_LEN = 40
  MIN_SIGNATURE_LEN = 10

  @staticmethod
  def generateLines(text):
    line_start_indent = 0
    cur_line = []
    for line in DialogLine.NL.split(text):
      indent = len(DialogLine.INDENT.match(line).group(1))
      if indent <= line_start_indent:
        if cur_line:
          yield ' '.join(cur_line)
        del cur_line[:]
        line_start_indent = indent
      cur_line.append(line.strip())
    if cur_line:
      yield ' '.join(cur_line)

  @staticmethod
  def parseLine(line):
    if not line:
      return (None, None, line)
    match = DialogLine.STATEMENT.match(line)
    if not match:
      return (None, None, line)
    data = match.groupdict()
    time = None
    if data['timestamp']:
      match = DialogLine.TIME.match(data['timestamp'])
      if match:
        tdata = match.groupdict(0)
        hour = int(tdata['hour'])
        minute = int(tdata['minute'])
        second = int(tdata['second'])
        if hour < 24 and minute < 60 and second < 60:
          time = datetime.time(hour, minute, second)
    return (time, data['actor'] or None, data['statement'])

  @staticmethod
  def parse(quote):
    for i, line in enumerate(DialogLine.generateLines(quote.dialog_source)):
      timestamp, actor, statement = DialogLine.parseLine(line)
      if not statement:
        logging.error('parseLine returned empty statement: %r', line)
        logging.info(repr(quote.dialog_source))
      yield DialogLine(parent=quote,
                       offset=i,
                       time=timestamp,
                       actor=actor,
                       text=statement,
                      )

  def getSignature(self):
    parts = []
    if self.actor:
      match = self.NICK.search(self.actor)
      if match:
        parts.append(match.group('nick'))
    total = -1
    words = self.WORD_SPLITTER.split(self.text)[:self.MAX_SIGNATURE_WORDS]
    for word in words:
      word = self.WORD_STRIPPER.sub('', word)
      total += len(word) + 1
      if total > self.MAX_SIGNATURE_LEN:
        if not parts:
          parts.append(word[:self.MAX_SIGNATURE_LEN])
        break
      parts.append(word)
    if total >= self.MIN_SIGNATURE_LEN:
      return ' '.join(parts)


class Quote(search.SearchableModel):
  draft = db.BooleanProperty(required=True, default=True)
  submitted = db.DateTimeProperty(required=True, auto_now_add=True)
  modified = db.DateTimeProperty()
  built = db.DateTimeProperty(default=datetime.datetime.fromtimestamp(0))
  context = db.ReferenceProperty(Context)
  dialog_source = db.TextProperty(required=True)
  note = db.TextProperty()
  signature = db.StringListProperty()
  legacy_id = db.IntegerProperty()

  @staticmethod
  def createDraft(account, source,
                  context=None,
                  note=None,
                  submitted=None,
                  legacy_id=None,
                 ):
    logging.info('creating draft by %r', account)
    kwargs = {}
    if submitted:
      kwargs['submitted'] = submitted
    quote = Quote(parent=account,
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

  @staticmethod
  def getDraft(account, key):
    logging.info('looking up draft: %s', key)
    draft = Quote.get(key)
    if not draft:
      raise InvalidKeyException
    if not draft.draft:
      raise InvalidQuoteStateException
    if account.key() != draft.parent_key():
      raise NoPermissionException
    return draft

  @staticmethod
  def getByLegacyId(legacy_id):
    return Quote.all().filter('legacy_id =', legacy_id).get()

  @staticmethod
  def getPublishedQuote(key):
    quote = Quote.get(key)
    if quote and not quote.draft:
      return quote

  @staticmethod
  def getRecentQuotes(reversed=False, **kwargs):
    return Quote.getQuotesByTimestamp('submitted', descending=not reversed, **kwargs)

  @staticmethod
  def getQuotesByBuildTime(**kwargs):
    return Quote.getQuotesByTimestamp('built', **kwargs)

  @staticmethod
  def getQuotesByTimestamp(property, start=None, offset=0, limit=10,
                           descending=False, include_drafts=True):
    logging.info('quotes by ts: property=%s, start=%s, offset=%s limit=%s, descending=%s, drafts=%s',
                 property, start, offset, limit, descending, include_drafts)
    query = Quote.all()
    if not include_drafts:
      query.filter('draft =', False)
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

  @staticmethod
  def getAccountQuotes(name, offset=0, limit=10, order='-submitted'):
    account = accounts.Account.getByName(name)
    if not account:
      return []
    query = (Quote.all()
             .ancestor(account)
             .filter('draft =', False)
             .order(order)
            )
    return list(query.fetch(offset=offset, limit=limit))

  @staticmethod
  def getDraftQuotes(account, offset=0, limit=10, order='-submitted'):
    query = (Quote.all()
             .ancestor(account)
             .filter('draft =', True)
             .order(order)
            )
    return list(query.fetch(offset=offset, limit=limit))

  @staticmethod
  def search(query, offset=0, limit=10):
    query = Quote.all().search(query).filter('draft =', False)
    return list(query.fetch(offset=offset, limit=limit))

  def publish(self, modified=None):
    if not self.draft:
      raise InvalidQuoteStateException
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

  def update(self, dialog=None, publish=False, modified=None):
    if not self.draft:
      raise InvalidQuoteStateException
    if dialog is not None:
      self.dialog_source = dialog
    self.rebuild()
    if publish:
      self.publish(modified=modified)
    else:
      system.record(self.parent(), VERB_UPDATED, self)

  def updateDialog(self):
    new_lines = list(DialogLine.parse(self))
    old_lines = list(self.getDialog(sorted=False))
    if old_lines:
      db.delete(old_lines)
    db.put(new_lines)
    signatures = [line.getSignature() for line in new_lines]
    self.signature = [s for s in signatures if s]
    self.put()
    return new_lines

  def getDialog(self, sorted=True):
    query = DialogLine.all().ancestor(self)
    if sorted:
      query.order('offset')
    return query

  def findDuplicates(self):
    scores = {}
    logging.info('Checking for dupes:')
    for line in self.signature:
      logging.info('  sig: %s', line)
      query = Quote.all().filter('signature =', line)
      for match in (l.key() for l in query):
        if self.key() != match:
          logging.info('    match: %s', match)
          scores.setdefault(match, 0)
          scores[match] += 1
    logging.info('matches: %s', scores)
    matches = scores.keys()
    matches.sort(cmp=lambda a, b: cmp(scores[b], scores[a]))
    return matches

  def rebuild(self):
    self.built = datetime.datetime.now()
    self.put()
    self.updateDialog()
