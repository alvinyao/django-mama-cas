from datetime import timedelta
import time
import logging
import re
import requests

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.http import same_origin
from django.contrib.auth.models import User

from mama_cas.exceptions import InvalidRequestError
from mama_cas.exceptions import InvalidTicketError
from mama_cas.exceptions import InvalidServiceError
from mama_cas.exceptions import InternalError
from mama_cas.exceptions import BadPGTError
from mama_cas.utils import add_query_params
from mama_cas.utils import is_scheme_https


LOG = logging.getLogger('mama_cas')

TICKET_RAND_LEN = getattr(settings, 'CAS_TICKET_RAND_LEN', 32)
TICKET_RE = re.compile("^[A-Z]{2,3}-[0-9]{10,}-[a-zA-Z0-9]{%d}$" % TICKET_RAND_LEN)


class TicketManager(models.Manager):
    def create_ticket(self, ticket=None, **kwargs):
        """
        Create a new ``Ticket`` with the appropriate default values. Any
        provided arguments are passed on to the ``create()`` function.
        Return the newly created ``Ticket``.
        """
        if not ticket:
            ticket = self.create_rand_str(prefix=self.model.TICKET_PREFIX)
        now = timezone.now()
        new_ticket = self.create(ticket=ticket, created=now, **kwargs)
        LOG.debug("Created %s %s" % (self.model._meta.verbose_name.title(), new_ticket.ticket))
        return new_ticket

    def create_rand_str(self, prefix=None):
        """
        Generate a sufficiently opaque ticket string to ensure the ticket is
        not guessable. If a prefix is provided, prepend it to the string.
        """
        if not prefix:
            prefix = self.model.TICKET_PREFIX
        return "%s-%d-%s" % (prefix, int(time.time()),
                             get_random_string(length=TICKET_RAND_LEN))

    def validate_ticket(self, ticket, service=None, renew=False):
        """
        Given a ticket string, validate the corresponding ``Ticket`` returning
        the ``Ticket`` if valid. If validation fails, return ``False``.

        If ``service`` is provided and the ticket has a service attribute,
        the origin of the two services will be compared. Validation will only
        succeed if the service origins match.

        if ``renew`` is provided, the validation will only succeed if the
        ticket was issued from the presentation of the user's primary
        credentials.
        """
        if not ticket:
            raise InvalidRequestError("No ticket string provided")

        # If the model contains a ``service`` field, make sure we received
        # a service parameter to compare against it
        try:
            self.model._meta.get_field('service')
        except models.FieldDoesNotExist:
            pass
        else:
            if not service:
                raise InvalidRequestError("No service identifier provided")

        if not TICKET_RE.match(ticket):
            raise InvalidTicketError("Ticket string %s is invalid" % ticket)

        title = self.model._meta.verbose_name.title()

        try:
            t = self.get(ticket=ticket)
        except self.model.DoesNotExist:
            raise InvalidTicketError("%s %s does not exist" % (title, ticket))

        if t.is_consumed():
            raise InvalidTicketError("%s %s has already been used" % (title, ticket))
        t.consume()

        if t.is_expired():
            raise InvalidTicketError("%s %s has expired" % (title, ticket))

        if service and hasattr(t, 'service'):
            if not same_origin(t.service, service):
                raise InvalidServiceError("%s %s for service %s is invalid for service %s" % (title, ticket, t.service, service))

        if renew and not t.is_primary():
            raise InvalidTicketError("%s %s was not issued via primary credentials" % (title, ticket))

        LOG.info("Validated %s %s" % (title, ticket))
        return t

    def delete_invalid_tickets(self):
        """
        Iterate over all ``Ticket``s and delete all consumed or expired
        ``Ticket``s. Invalid tickets are no longer valid for future
        authentication attempts and can be safely deleted.

        A custom management command is provided that executes this method
        on all applicable models by running ``manage.py cleanupcas``. It
        is recommended that you run this command on a regular basis to
        prevent invalid tickets from causing storage or performance issues.
        """
        for ticket in self.all():
            if ticket.is_consumed() or ticket.is_expired():
                ticket.delete()

    def consume_tickets(self, user):
        """
        Iterate over all ``Ticket``s for a specified user and consume all
        tickets if they are not already consumed or expired. This is used
        when the user logs out to ensure all tickets issued for this user
        are no longer valid for future authentication attempts.
        """
        for ticket in self.filter(user=user):
            if not ticket.is_consumed() and not ticket.is_expired():
                ticket.consume()

class Ticket(models.Model):
    """
    ``Ticket`` is an abstract base class implementing common methods
    and fields for the assorted ticket types. It should never be
    interacted with directly within the application.

    It is recommended that you do not interact directly with this model
    or its inheritors. Instead, the provided manager contains methods
    for creating, validating, consuming and deleting invalid ``Ticket``s.
    """
    TICKET_EXPIRE = getattr(settings, 'CAS_SERVICE_TICKET_EXPIRE', 5)

    ticket = models.CharField(max_length=255, unique=True)
    user = models.ForeignKey(User)
    created = models.DateTimeField()
    consumed = models.DateTimeField(null=True)

    objects = TicketManager()

    class Meta:
        abstract = True

    def __unicode__(self):
        return u'%s' % unicode(self.ticket)

    def consume(self):
        """
        A ``Ticket`` is consumed by populating the ``consumed`` field with
        the current datetime. A consumed ``Ticket`` is no longer valid for
        any future authentication attempts.
        """
        self.consumed = timezone.now()
        self.save()

    def is_consumed(self):
        """
        Check a ``Ticket``s consumed state. Return ``True`` if the ticket is
        consumed, and ``False`` otherwise.
        """
        if self.consumed:
            return True
        return False

    def is_expired(self):
        """
        Check a ``Ticket``s expired state. Return ``True`` if the ticket is
        expired, and ``False`` otherwise.
        """
        if self.created + timedelta(minutes=self.TICKET_EXPIRE) <= timezone.now():
            return True
        return False

class ServiceTicket(Ticket):
    """
    (3.1) A ``ServiceTicket`` is used by the client as a credential to
    obtain access to a service. It is obtained upon a client's presentation
    of credentials and a service identifier to /login.
    """
    TICKET_PREFIX = u"ST"

    service = models.CharField(max_length=255)
    primary = models.BooleanField()

    class Meta:
        verbose_name = "service ticket"
        verbose_name_plural = "service tickets"

    def is_primary(self):
        """
        Check the credential origin for a ``ServiceTicket``. If the ticket was
        issued from the presentation of the user's primary credentials,
        return ``True``, otherwise return ``False``.
        """
        if self.primary:
            return True
        return False

class ProxyTicket(Ticket):
    """
    (3.2) A ``ProxyTicket`` is used by a service as a credential to obtain
    access to a back-end service on behalf of a client. It is obtained upon
    a service's presentation of a ``ProxyGrantingTicket`` and a service
    identifier.
    """
    TICKET_PREFIX = u"PT"

    service = models.CharField(max_length=255)
    granted_by_pgt = models.ForeignKey('ProxyGrantingTicket', null=True, blank=True)

    class Meta:
        verbose_name = "proxy ticket"
        verbose_name_plural = "proxy tickets"

class ProxyGrantingTicketManager(TicketManager):
    def create_ticket(self, pgturl, **kwargs):
        """
        When a ``pgtUrl`` parameter is provided to ``/serviceValidate`` or
        ``/proxyValidate``, attempt to create a new ``ProxyGrantingTicket``.
        Start by creating the necessary ticket strings and then validate the
        callback URL. If validation succeeds, create and return the
        ``ProxyGrantingTicket``. If validation fails, return ``None``.
        """
        pgtid = self.create_rand_str(prefix=self.model.TICKET_PREFIX)
        pgtiou = self.create_rand_str(prefix=self.model.IOU_PREFIX)
        try:
            self.validate_pgturl(pgturl, pgtid, pgtiou)
        except InternalError as e:
            # pgtUrl validation failed, so nothing has been created
            LOG.warn("%s %s" % (e.code, e))
            return None
        else:
            # pgtUrl validation succeeded, so create a new PGT with the
            # already created ticket strings
            return super(ProxyGrantingTicketManager, self).create_ticket(ticket=pgtid, iou=pgtiou, **kwargs)

    def validate_pgturl(self, pgturl, pgtid, pgtiou):
        """
        Verify the provided proxy callback URL. This verification process
        requires three steps:

        1. The URL scheme must be HTTPS
        2. The SSL certificate must be valid and its name must match that
           of the service
        3. The callback URL must respond with a 200 or 3xx response code

        It is not required for validation that 3xx redirects be followed.
        """
        # Ensure the scheme is HTTPS before proceeding
        if not is_scheme_https(pgturl):
            raise InternalError("Proxy callback URL scheme is not HTTPS")

        # Connect to proxy callback URL, checking the SSL certificate
        pgturl = add_query_params(pgturl, { 'pgtId': pgtid, 'pgtIou': pgtiou })
        try:
            r = requests.get(pgturl, verify=True)
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as e:
            raise InternalError("%s" % e)

        # Check the returned HTTP status code
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise InternalError("Proxy callback returned %s" % e)

    def validate_ticket(self, ticket, service):
        """
        Given a ticket string, validate the corresponding ``Ticket`` returning
        the ``Ticket`` if valid. If validation fails, return ``False``.
        """
        if not ticket:
            raise InvalidRequestError("No ticket string provided")

        if not service:
            raise InvalidRequestError("No service identifier provided")

        if not TICKET_RE.match(ticket):
            raise InvalidTicketError("Ticket string %s is invalid" % ticket)

        title = self.model._meta.verbose_name.title()

        try:
            t = self.get(ticket=ticket)
        except self.model.DoesNotExist:
            raise BadPGTError("%s %s does not exist" % (title, ticket))

        if t.is_consumed():
            raise InvalidTicketError("%s %s has already been used" % (title, ticket))

        if t.is_expired():
            raise InvalidTicketError("%s %s has expired" % (title, ticket))

        LOG.info("Validated %s %s" % (title, ticket))
        return t

class ProxyGrantingTicket(Ticket):
    """
    (3.3) A ``ProxyGrantingTicket`` is used by a service to obtain proxy
    tickets for obtaining access to a back-end service on behalf of a
    client. It is obtained upon validation of a ``ServiceTicket`` or a
    ``ProxyTicket``.
    """
    TICKET_PREFIX = u"PGT"
    IOU_PREFIX = u"PGTIOU"

    iou = models.CharField(max_length=255, unique=True)
    granted_by_st = models.ForeignKey(ServiceTicket, null=True, blank=True)
    granted_by_pt = models.ForeignKey(ProxyTicket, null=True, blank=True)

    objects = ProxyGrantingTicketManager()

    class Meta:
        verbose_name = "proxy-granting ticket"
        verbose_name_plural = "proxy-granting tickets"
