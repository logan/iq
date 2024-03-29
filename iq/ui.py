import logging
import os
import re
import urllib
import wsgiref.handlers

from google.appengine.ext import webapp
from google.appengine.ext.webapp import template

import accounts
import browse
import facebook
import quotes
import service
import system

def ui(path, **kwargs):
  tpath = os.path.join('templates', path)
  service_dec = service.service(**kwargs)
  def decorator(f):
    f = service_dec(f)
    def wrapper(self):
      def pre_hook():
        self.facebook = facebook.FacebookSupport(self)
        self.template.stability_level = str(system.getSystem().stability_level)
        if self.account.trusted:
          self.template.delete_return_url = self.request.url
          self.template.draft_page = browse.PageSpecifier(mode='draft')
          self.template.my_page = browse.PageSpecifier(mode='recent',
                                                       account=self.account)
      tmpl = service.Template()
      f(self, template=tmpl, pre_hook=pre_hook)
      if self.status == 200:
        self.response.out.write(template.render(tpath, tmpl.__dict__, debug=True))
    return wrapper
  return decorator


class IndexPage(browse.BrowseService):
  @ui('browse.html')
  def get(self):
    self.browseQuotes(browse.PageSpecifier(mode='recent'))


class QuotePage(service.QuoteService):
  PATH_PATTERN = re.compile(r'^/q/(?P<account>\d+)/(?P<quote>\d+)$')

  @ui('quote.html')
  def get(self):
    self.template.delete_return_url = ''
    match = self.PATH_PATTERN.match(self.request.path)
    if match:
      account_id = int(match.group('account'))
      quote_id = int(match.group('quote'))
      account = accounts.Account.getByShortId(account_id)
      quote = quotes.Quote.getQuoteByShortId(id=quote_id,
                                             parent=account,
                                             account=self.account,
                                            )
      self.template.quote = quote
    else:
      quote = self.getQuote()
    if quote:
      quote.owner_page = browse.PageSpecifier(mode='recent',
                                              account=quote.parent())
      if self.account.trusted:
        rating = quote.getAccountRating(self.account)
        if rating:
          quote.account_rating = str(rating.value)
        else:
          quote.account_rating = ''
    else:
      self.status = 404


class BrowsePage(browse.BrowseService):
  @ui('browse.html')
  def get(self):
    self.browseQuotes(browse.PageSpecifier(mode='recent'))
    if self.template.quotes:
      for quote in self.template.quotes:
        quote.owner_page = browse.PageSpecifier(mode='recent',
                                                account=quote.parent())
        if self.account.trusted:
          rating = quote.getAccountRating(self.account)
          if rating:
            quote.account_rating = str(rating.value)
          else:
            quote.account_rating = ''


class SearchPage(browse.BrowseService):
  @ui('browse.html')
  def get(self):
    self.browseQuotes(browse.PageSpecifier(mode='search'))
    if self.template.quotes:
      for quote in self.template.quotes:
        quote.owner_page = browse.PageSpecifier(mode='recent',
                                                account=quote.parent())


class SubmitPage(service.CreateDraftService):
  @ui('submit.html', require_trusted=True)
  def get(self):
    pass

  @ui('submit.html', require_trusted=True)
  def post(self):
    draft = self.createDraft()
    if draft:
      self.redirect('/edit-draft?key=%s' % urllib.quote(str(draft.key())))


class EditPage(service.EditService):
  @ui('quote.html', require_trusted=True)
  def get(self):
    draft = self.edit()
    self.redirect('/edit-draft?key=%s' % urllib.quote(str(draft.key())))


class EditDraftPage(service.EditDraftService):
  @ui('edit-draft.html', require_trusted=True)
  def get(self):
    self.template.preview = True
    self.getDraft()

  @ui('edit-draft.html', require_trusted=True)
  def post(self):
    self.template.preview = True
    if self.request.get('save'):
      self.save()
    elif self.request.get('discard'):
      self.discard()
    elif self.request.get('publish'):
      self.publish()


class DeleteQuotePage(service.DeleteQuoteService):
  @ui('delete.html')
  def get(self):
    self.template.return_url = self.request.get('return_url')
    self.template.preview = True
    self.getQuote()

  @ui('delete.html')
  def post(self):
    if self.delete():
      return_url = self.request.get('return_url')
      if return_url:
        self.redirect(return_url)


class CreateAccountPage(service.CreateAccountService):
  @ui('create-account.html')
  def get(self):
    pass


class ActivationPage(service.ActivationService):
  @ui('activate.html')
  def get(self):
    if self.activate():
      self.redirect('/')

  @ui('activate.html')
  def post(self):
    if self.activate():
      self.redirect('/')


class ResetPasswordPage(service.ResetPasswordService):
  @ui('reset-password.html')
  def get(self):
    self.reset()

  @ui('reset-password.html')
  def post(self):
    if self.reset():
      self.redirect('/')


class LogoutPage(service.LogoutService):
  @ui('index.html')
  def get(self):
    self.logout()


def main():
  pages = [
    ('/', BrowsePage),
    ('/activate', ActivationPage),
    ('/browse', BrowsePage),
    ('/create-account', CreateAccountPage),
    ('/delete', DeleteQuotePage),
    ('/edit', EditPage),
    ('/edit-draft', EditDraftPage),
    ('/quote', QuotePage),
    ('/reset-password', ResetPasswordPage),
    ('/search', SearchPage),
    ('/submit', SubmitPage),
    ('/logout', LogoutPage),
    (r'/q/\d+/\d+', QuotePage),
  ]
  application = webapp.WSGIApplication(pages, debug=True)
  wsgiref.handlers.CGIHandler().run(application)


if __name__ == '__main__':
  main()
