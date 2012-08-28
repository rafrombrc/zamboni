from datetime import datetime

from django.conf import settings
from django.utils.datastructures import SortedDict

import commonware.log
from tower import ugettext_lazy as _lazy

import amo
from access import acl
from amo.helpers import absolutify
from amo.urlresolvers import reverse
from amo.utils import send_mail_jinja
from editors.models import EscalationQueue, ReviewerScore
from files.models import File

from .models import RereviewQueue


log = commonware.log.getLogger('z.mailer')


def send_mail(subject, template, context, emails, perm_setting=None, cc=None):
    # Link to our newfangled "Account Settings" page.
    manage_url = absolutify(reverse('account.settings')) + '#notifications'
    send_mail_jinja(subject, template, context, recipient_list=emails,
                    from_email=settings.NOBODY_EMAIL, use_blacklist=False,
                    perm_setting=perm_setting, manage_url=manage_url,
                    headers={'Reply-To': settings.MKT_REVIEWERS_EMAIL}, cc=cc)


class ReviewBase(object):

    def __init__(self, request, addon, version, review_type):
        self.request = request
        self.user = self.request.user
        self.addon = addon
        self.version = version
        self.review_type = review_type
        self.files = None
        self.in_pending = self.addon.status == amo.STATUS_PENDING
        self.in_rereview = RereviewQueue.objects.filter(
            addon=self.addon).exists()
        self.in_escalate = EscalationQueue.objects.filter(
            addon=self.addon).exists()

    def set_addon(self, **kw):
        """Alters addon and sets reviewed timestamp on version."""
        self.addon.update(**kw)
        self.version.update(reviewed=datetime.now())

    def set_files(self, status, files, copy_to_mirror=False,
                  hide_disabled_file=False):
        """Change the files to be the new status
        and copy, remove from the mirror as appropriate."""
        for file in files:
            file.datestatuschanged = datetime.now()
            file.reviewed = datetime.now()
            if copy_to_mirror:
                file.copy_to_mirror()
            if hide_disabled_file:
                file.hide_disabled_file()
            file.status = status
            file.save()

    def log_action(self, action):
        details = {'comments': self.data['comments'],
                   'reviewtype': self.review_type}
        if self.files:
            details['files'] = [f.id for f in self.files]

        amo.log(action, self.addon, self.version, user=self.user.get_profile(),
                created=datetime.now(), details=details)

    def notify_email(self, template, subject):
        """Notify the authors that their app has been reviewed."""
        emails = list(self.addon.authors.values_list('email', flat=True))
        cc_email = self.addon.mozilla_contact or None
        data = self.data.copy()
        data.update(self.get_context_data())
        data['tested'] = ''
        os, app = data.get('operating_systems'), data.get('applications')
        if os and app:
            data['tested'] = 'Tested on %s with %s' % (os, app)
        elif os and not app:
            data['tested'] = 'Tested on %s' % os
        elif not os and app:
            data['tested'] = 'Tested with %s' % app
        send_mail(subject % self.addon.name,
                  'reviewers/emails/decisions/%s.txt' % template, data,
                  emails, perm_setting='app_reviewed', cc=cc_email)

    def get_context_data(self):
        return {'name': self.addon.name,
                'reviewer': self.request.user.get_profile().name,
                'detail_url': absolutify(
                    self.addon.get_url_path(add_prefix=False)),
                'review_url': absolutify(reverse('reviewers.apps.review',
                                                 args=[self.addon.app_slug],
                                                 add_prefix=False)),
                'status_url': absolutify(self.addon.get_dev_url('versions')),
                'comments': self.data['comments'],
                'MKT_SUPPORT_EMAIL': settings.MKT_SUPPORT_EMAIL,
                'SITE_URL': settings.SITE_URL}

    def request_information(self):
        """Send a request for information to the authors."""
        emails = list(self.addon.authors.values_list('email', flat=True))
        cc_email = self.addon.mozilla_contact or None
        self.log_action(amo.LOG.REQUEST_INFORMATION)
        self.version.update(has_info_request=True)
        log.info(u'Sending request for information for %s to %s' %
                 (self.addon, emails))
        send_mail(u'Submission Update: %s' % self.addon.name,
                  'reviewers/emails/decisions/info.txt',
                  self.get_context_data(), emails,
                  perm_setting='app_individual_contact', cc=cc_email)

    def send_escalate_mail(self):
        self.log_action(amo.LOG.ESCALATE_MANUAL)
        log.info(u'Escalated review requested for %s' % self.addon)
        send_mail(u'Escalated Review Requested: %s' % self.addon.name,
                  'reviewers/emails/super_review.txt',
                  self.get_context_data(), [settings.MKT_SENIOR_EDITORS_EMAIL])


class ReviewApp(ReviewBase):

    def set_data(self, data):
        self.data = data
        self.files = self.version.files.all()

    def process_public(self):
        if self.addon.make_public == amo.PUBLIC_IMMEDIATELY:
            self.process_public_immediately()
        else:
            self.process_public_waiting()

        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()

        # Assign reviewer incentive scores.
        event = ReviewerScore.get_event_by_type(self.addon)
        ReviewerScore.award_points(self.request.amo_user, self.addon, event)

    def process_public_waiting(self):
        """Make an app pending."""
        self.set_files(amo.STATUS_PUBLIC_WAITING, self.version.files.all())
        if self.addon.status != amo.STATUS_PUBLIC:
            self.set_addon(status=amo.STATUS_PUBLIC_WAITING,
                           highest_status=amo.STATUS_PUBLIC_WAITING)

        self.log_action(amo.LOG.APPROVE_VERSION_WAITING)
        self.notify_email('pending_to_public_waiting',
                          u'App Approved but waiting: %s')

        log.info(u'Making %s public but pending' % self.addon)
        log.info(u'Sending email for %s' % self.addon)

    def process_public_immediately(self):
        """Approve an app."""
        # Save files first, because set_addon checks to make sure there
        # is at least one public file or it won't make the addon public.
        self.set_files(amo.STATUS_PUBLIC, self.version.files.all())
        if self.addon.status != amo.STATUS_PUBLIC:
            self.set_addon(status=amo.STATUS_PUBLIC,
                           highest_status=amo.STATUS_PUBLIC)

        self.log_action(amo.LOG.APPROVE_VERSION)
        self.notify_email('pending_to_public', u'App Approved: %s')

        log.info(u'Making %s public' % self.addon)
        log.info(u'Sending email for %s' % self.addon)

    def process_sandbox(self):
        """Reject an app."""
        self.set_files(amo.STATUS_DISABLED, self.version.files.all(),
                       hide_disabled_file=True)
        # If this app is not packaged (packaged apps can have multiple
        # versions) or if there aren't other versions with already reviewed
        # files, reject the app also.
        if (not self.addon.is_packaged or
            not self.addon.versions.exclude(id=self.version.id)
                .filter(files__status__in=amo.REVIEWED_STATUSES).exists()):
            self.set_addon(status=amo.STATUS_REJECTED)

        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()
        if self.in_rereview:
            RereviewQueue.objects.filter(addon=self.addon).delete()

        self.log_action(amo.LOG.REJECT_VERSION)
        self.notify_email('pending_to_sandbox', u'Submission Update: %s')

        log.info(u'Making %s disabled' % self.addon)
        log.info(u'Sending email for %s' % self.addon)

    def process_escalate(self):
        """Ask for escalation for an app."""
        EscalationQueue.objects.get_or_create(addon=self.addon)
        self.notify_email('author_super_review', u'Submission Update: %s')

        self.send_escalate_mail()

    def process_comment(self):
        self.version.update(has_editor_comment=True)
        self.log_action(amo.LOG.COMMENT_VERSION)

    def process_clear_escalation(self):
        """Clear app from escalation queue."""
        EscalationQueue.objects.filter(addon=self.addon).delete()
        self.log_action(amo.LOG.ESCALATION_CLEARED)
        log.info(u'Escalation cleared for app: %s' % self.addon)

    def process_clear_rereview(self):
        """Clear app from re-review queue."""
        RereviewQueue.objects.filter(addon=self.addon).delete()
        self.log_action(amo.LOG.REREVIEW_CLEARED)
        log.info(u'Re-review cleared for app: %s' % self.addon)

    def process_disable(self):
        """Disables app."""
        if not acl.action_allowed(self.request, 'Addons', 'Edit'):
            return

        # Disable disables all files, not just those in this version.
        self.set_files(amo.STATUS_DISABLED,
                       File.objects.filter(version__addon=self.addon),
                       hide_disabled_file=True)
        self.addon.update(status=amo.STATUS_DISABLED)
        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()
        if self.in_rereview:
            RereviewQueue.objects.filter(addon=self.addon).delete()
        emails = list(self.addon.authors.values_list('email', flat=True))
        cc_email = self.addon.mozilla_contact or None
        send_mail(u'App disabled by reviewer: %s' % self.addon.name,
                  'reviewers/emails/decisions/disabled.txt',
                  self.get_context_data(), emails,
                  perm_setting='app_individual_contact', cc=cc_email)
        self.log_action(amo.LOG.APP_DISABLED)
        log.info(u'App %s has been disabled by a reviewer.' % self.addon)


class ReviewHelper(object):
    """
    A class that builds enough to render the form back to the user and
    process off to the correct handler.
    """

    def __init__(self, request=None, addon=None, version=None):
        self.handler = None
        self.required = {}
        self.addon = addon
        self.version = version
        self.all_files = version and version.files.all()
        self.get_review_type(request, addon, version)
        self.actions = self.get_actions()

    def set_data(self, data):
        self.handler.set_data(data)

    def get_review_type(self, request, addon, version):
        if EscalationQueue.objects.filter(addon=addon).exists():
            queue = 'escalated'
        elif RereviewQueue.objects.filter(addon=addon).exists():
            queue = 'rereview'
        else:
            queue = 'pending'
        self.review_type = queue
        self.handler = ReviewApp(request, addon, version, queue)

    def get_actions(self):
        public = {
            'method': self.handler.process_public,
            'minimal': False,
            'label': _lazy(u'Push to public'),
            'details': _lazy(u'This will approve the sandboxed app so it '
                             u'appears on the public side.')}
        reject = {
            'method': self.handler.process_sandbox,
            'label': _lazy(u'Reject'),
            'minimal': False,
            'details': _lazy(u'This will reject the app and remove it from '
                             u'the review queue.')}
        info = {
            'method': self.handler.request_information,
            'label': _lazy(u'Request more information'),
            'minimal': True,
            'details': _lazy(u'This will send the author(s) an email '
                             u'requesting more information.')}
        escalate = {
            'method': self.handler.process_escalate,
            'label': _lazy(u'Escalate'),
            'minimal': True,
            'details': _lazy(u'Flag this app for an admin to review.')}
        comment = {
            'method': self.handler.process_comment,
            'label': _lazy(u'Comment'),
            'minimal': True,
            'details': _lazy(u'Make a comment on this app.  The author won\'t '
                             u'be able to see this.')}
        clear_escalation = {
            'method': self.handler.process_clear_escalation,
            'label': _lazy(u'Clear Escalation'),
            'minimal': True,
            'details': _lazy(u'Clear this app from the escalation queue. The '
                             u'author will get no email or see comments '
                             u'here.')}
        clear_rereview = {
            'method': self.handler.process_clear_rereview,
            'label': _lazy(u'Clear Re-review'),
            'minimal': True,
            'details': _lazy(u'Clear this app from the re-review queue. The '
                             u'author will get no email or see comments '
                             u'here.')}
        disable = {
            'method': self.handler.process_disable,
            'label': _lazy(u'Disable app'),
            'minimal': True,
            'details': _lazy(u'Disable the app, removing it from public '
                             u'results. Sends comments to author.')}

        actions = SortedDict()

        file_status = self.version.files.values_list('status', flat=True)

        # Public.
        if (self.addon.status != amo.STATUS_PUBLIC or
            amo.STATUS_PUBLIC not in file_status):
            actions['public'] = public

        # Reject.
        if (self.addon.status != amo.STATUS_REJECTED and
            self.addon.status != amo.STATUS_DISABLED or (
                amo.STATUS_REJECTED not in file_status and
                amo.STATUS_DISABLED not in file_status)):
            actions['reject'] = reject

        # Disable.
        if (acl.action_allowed(self.handler.request, 'Addons', 'Edit') and (
                self.addon.status != amo.STATUS_DISABLED or
                amo.STATUS_DISABLED not in file_status)):
            actions['disable'] = disable

        # Clear escalation.
        if self.handler.in_escalate:
            actions['clear_escalation'] = clear_escalation

        # Clear re-review.
        if self.handler.in_rereview:
            actions['clear_rereview'] = clear_rereview

        # Escalate.
        if not self.handler.in_escalate:
            actions['escalate'] = escalate

        # Request info and comment are always shown.
        actions['info'] = info
        actions['comment'] = comment

        return actions

    def process(self):
        action = self.handler.data.get('action', '')
        if not action:
            raise NotImplementedError
        return self.actions[action]['method']()
