import datetime
import logging
import re

from google.appengine.api import mail
from google.appengine.ext import db

import hash
import mailer
import system

ACTIVATION_EMAIL_TEMPLATE = '''Dear %(name)s,

Welcome to IrcQuotes!  Before you can log into the site, you will need to
activate your account.  Simply visit the URL below to activate your account:

%(base_url)s/activate?id=%(id)s&activation=%(activation)s

Thank you for registering!
IrcQuotes Administration'''


class AccountException(Exception):
  pass


class InvalidName(AccountException):
  INVALID_CHARACTER = ('An account name may only contain letters, numerals,'
                       ' apostrophes, spaces, and other characters acceptable'
                       ' in IRC nicks.')
  MISSING_LETTER = 'An account name must contain at least one letter.'
  TOO_LONG = 'An account name may only be at most %d characters in length.'
  IN_USE = 'This name is already in use.'


class InvalidEmail(AccountException):
  INVALID_FORMAT = "This doesn't look like a valid email address."
  TOO_LONG = 'We only support email addresses up to %d characters long.'
  IN_USE = 'This email is already in use.'


class NoSuchAccountException(AccountException): pass
class InvalidPasswordException(AccountException): pass
class NotActivatedException(AccountException): pass
class InvalidAccountStateException(AccountException): pass
class InvalidActivationException(AccountException): pass


VERB_SIGNED_UP = system.Verb('signed up')

@system.capture(VERB_SIGNED_UP)
def onAccountActivated(action):
  system.incrementAccountCount()


class Account(db.Expando):
  # Unique identifier for this account
  #   Examples:
  #     iq/logan
  #     facebook/1234567
  id = db.StringProperty(required=True)

  # Another unique identifier, but not every account necessarily has one.
  # All stored emails should be lowercased before storage.
  email = db.EmailProperty()

  # For ids in the iq namespace, this is required to log in.
  password = db.StringProperty()

  # Access control
  trusted = db.BooleanProperty(default=False)
  admin = db.BooleanProperty(default=False)

  # Publicly displayed name for the account.  Details from the id may also
  # be displayed (such as the fact that the user comes from Facebook).
  name = db.StringProperty(required=True)

  # Timestamps
  created = db.DateTimeProperty(required=True, auto_now_add=True)
  activated = db.DateTimeProperty()
  active = db.DateTimeProperty(required=True, auto_now=True)

  # Account activation support
  activation = db.StringProperty()
  activation_url = db.StringProperty()

  # Migration support
  legacy_id = db.IntegerProperty()

  # Counters
  quote_count = db.IntegerProperty(default=0)
  draft_count = db.IntegerProperty(default=0)

  MAX_NAME_LENGTH = 20
  NAME_INVALID_CHARACTER_PATTERN = re.compile(r"[^\w\d'\[\]{}\\| -]")
  NAME_LETTER_PATTERN = re.compile(r'\w')

  MAX_EMAIL_LENGTH = 32
  EMAIL_PATTERN = re.compile(r'.+@.+\...+')

  def put(self):
    self.id = self.id.lower()
    if self.email:
      self.email = self.email.lower()
    return db.Model.put(self)

  @classmethod
  def validateName(cls, name):
    name = name.strip()
    if cls.NAME_INVALID_CHARACTER_PATTERN.search(name):
      raise InvalidName(InvalidName.INVALID_CHARACTER)
    if cls.NAME_LETTER_PATTERN.search(name) is None:
      raise InvalidName(InvalidName.MISSING_LETTER)
    if len(name) > cls.MAX_NAME_LENGTH:
      raise InvalidName(InvalidName.TOO_LONG % cls.MAX_NAME_LENGTH)
    if cls.getById('iq/%s' % name):
      raise InvalidName(InvalidName.IN_USE)

  @classmethod
  def validateEmail(cls, email):
    email = email.strip()
    if cls.EMAIL_PATTERN.match(email) is None:
      raise InvalidEmail(InvalidEmail.INVALID_FORMAT)
    if len(email) > cls.MAX_EMAIL_LENGTH:
      raise InvalidEmail(InvalidEmail.TOO_LONG % cls.MAX_EMAIL_LENGTH)
    if cls.getByEmail(email):
      raise InvalidEmail(InvalidEmail.IN_USE)

  @classmethod
  def getById(cls, id):
    query = cls.all().filter('id =', id.lower())
    return query.get()

  @classmethod
  def getByLegacyId(cls, legacy_id):
    return cls.all().filter('legacy_id =', legacy_id).get()

  @classmethod
  def getByEmail(cls, email):
    email = email.strip().lower()
    logging.info("Looking up account by email: %r", email)
    return cls.all().filter('email =', email).get()

  @classmethod
  def getAnonymous(cls):
    account = cls.getById('iq/anonymous')
    if account is None:
      account = cls(id='iq/anonymous', name='Anonymous')
      account.put()
    return account

  @classmethod
  def activate(cls, id, activation):
    logging.info('Attempting to activate %r', id)
    account = cls.getById(id)
    if not account:
      raise NoSuchAccountException
    if not account.activation:
      raise InvalidAccountStateException
    if account.activation != activation:
      raise InvalidActivationException
    account.activated = datetime.datetime.now()
    account.activation = None
    account.trusted = True
    account.put()
    system.record(account, VERB_SIGNED_UP)
    return account

  @classmethod
  def login(cls, id, password):
    hashpw = hash.generate(password)
    account = cls.getById(id)
    if not account or not account.trusted:
      raise NoSuchAccountException
    if account.activated is None and account.password is None:
      raise NotActivatedException
    if account.password != password and account.password != hashpw:
      raise InvalidPasswordException
    return account

  @classmethod
  def createIq(cls, name, email, password):
    name = name.strip()
    account = cls(id='iq/%s' % name.lower(),
                  name=name,
                  email=email.strip().lower(),
                  password=password,
                 )

    account.put()
    return account

  @classmethod
  def createLegacy(cls, user_id, name, email, password, created):
    account = cls(id='iq/%s' % name.lower(),
                  email=email.lower(),
                  password=password,
                  created=created,
                  activated=datetime.datetime.now(),
                  legacy_id=user_id,
                  trusted=True,
                 )
    account.put()
    system.incrementAccountCount()
    return account

  @classmethod
  def createFacebook(cls, facebook_id, name):
    account = cls(id='facebook/%s' % facebook_id,
                  name=name,
                  activated=datetime.datetime.now(),
                  trusted=True,
                 )
    account.put()
    system.record(account, VERB_SIGNED_UP)
    return account

  def setupActivation(self, mailer, base_url):
    if not self.activation:
      self.activation = hash.generate()
      self.put()
      logging.info("Activating account: id=%r, email=%r, activation=%r",
                   self.id, self.email, self.activation)
      self.sendConfirmationEmail(mailer, base_url)

  def sendConfirmationEmail(self, mailer, base_url):
    mailer.send(account=self,
                subject='IrcQuotes Account Activation',
                body=ACTIVATION_EMAIL_TEMPLATE % {
                  'id': self.id,
                  'name': self.name,
                  'activation': self.activation,
                  'base_url': base_url,
                })

  def setPassword(self, password):
    self.password = hash.generate(password)
    self.activation = None
    self.put()

  def isAdmin(self):
    if self.admin:
      return True
    if not self.trusted:
      return False
    sys = system.getSystem()
    if sys.owner:
      return False
    logging.info('Making %s owner and admin', self.name)
    sys.owner = self.name
    sys.put()
    self.admin = True
    self.put()
    return True

  def __repr__(self):
    tags = []
    if not self.trusted:
      tags.append('untrusted')
    if self.admin:
      tags.append('admin')
    return '<Account: %r%s %r>' % (self.id,
                                   tags and (' %s' % ', '.join(tags)) or '',
                                   self.name)


class Session(db.Expando):
  LIFETIME_DAYS = 14

  id = db.StringProperty(required=True)
  account = db.ReferenceProperty(Account)
  created = db.DateTimeProperty(required=True, auto_now_add=True)
  active = db.DateTimeProperty(required=True, auto_now=True)

  @staticmethod
  def expireAll():
    now = datetime.datetime.now()
    expiration = now - datetime.timedelta(days=Session.LIFETIME_DAYS)
    query = Session.all().filter("created <", expiration)
    for session in query:
      session.delete()
    logging.info("Deleted sessions: %d", query.count())

  @staticmethod
  def load(session_id):
    logging.info("Loading session: %s", session_id)
    session = Session.get_by_key_name(session_id)
    if session is None:
      logging.info("Creating new session: %s", session_id)
      session = Session(key_name=session_id,
                        id=session_id,
                        account=Account.getAnonymous(),
                       )
      session.put()
    return session

  @staticmethod
  def deleteAllEntities():
    query = Session.all().fetch(limit=100)
    for i, session in enumerate(query):
      session.delete()
    return i == 100
