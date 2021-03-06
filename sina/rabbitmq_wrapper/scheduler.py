import logging
import signal
import time

from source.sina.rabbitmq_wrapper.queue import RabbitMQQueue

logger = logging.getLogger(__name__)


class IScheduler(object):
    """ Base Scrapy scheduler class. """

    def __init__(self):
        raise NotImplementedError

    def open(self, spider):
        """Start scheduling"""
        raise NotImplementedError

    def close(self, reason):
        """Stop scheduling"""
        raise NotImplementedError

    def enqueue_request(self, request):
        """Add request to queue"""
        raise NotImplementedError

    def next_request(self):
        """Pop a request"""
        raise NotImplementedError

    def has_pending_requests(self):
        """Check if queue is not empty"""
        raise NotImplementedError


class Scheduler(IScheduler):
    # TODO: to be extended in future
    @staticmethod
    def _ensure_settings(settings, key):
        if not settings.get(key):
            msg = 'Please set "{}" at settings.'.format(key)
            raise ValueError(msg)


repo_url = 'https://github.com/mbriliauskas/scrapy-rabbitmq-link'


class RabbitMQScheduler(Scheduler):
    """ A RabbitMQ Scheduler for Scrapy. """
    queue = None
    stats = None

    def __init__(self, connection_url, max_request, *args, **kwargs):
        self.connection_url = connection_url
        self.max_request = max_request
        self.waiting = False
        self.closing = False
        persist = True
        idle_before_close = 0

    @classmethod
    def from_settings(cls, settings):
        cls._ensure_settings(settings, 'RABBITMQ_CONNECTION_PARAMETERS')
        connection_url = settings.get('RABBITMQ_CONNECTION_PARAMETERS')
        max_request = settings.get('CONCURRENT_REQUESTS')
        return cls(connection_url, max_request)

    @classmethod
    def from_crawler(cls, crawler):
        scheduler = cls.from_settings(crawler.settings)
        scheduler.stats = crawler.stats
        signal.signal(signal.SIGINT, scheduler.on_sigint)
        return scheduler

    def __len__(self):
        return len(self.queue)

    def open(self, spider):
        if not hasattr(spider, '_make_request'):
            msg = 'Method _make_request not found in spider. '
            msg += 'Please add it to spider or see manual at '
            msg += repo_url
            raise NotImplementedError(msg)

        if not hasattr(spider, 'queue_name'):
            msg = 'Please set queue_name parameter to spider. '
            msg += 'Consult manual at ' + repo_url
            raise ValueError(msg)

        self.spider = spider
        self.queue = self._make_queue(spider.exchange_name, spider.queue_name, spider.route_key)

        msg_count = len(self.queue)
        if msg_count:
            logger.info('Resuming crawling ({} urls scheduled)'
                        .format(msg_count))
        else:
            logger.info('No items to crawl in {}'
                        .format(self.queue.queue_name))

    def _make_queue(self, exchange, queue, route_key):
        return RabbitMQQueue(self.connection_url, exchange, queue, route_key, self.max_request)

    def on_sigint(self, signal, frame):
        self.closing = True

    def close(self, reason):
        try:
            self.queue.close()
            self.fwd_queue.close()
        except:
            pass

    def enqueue_request(self, request):
        """ Enqueues request to main queues back
        """
        if self.queue:
            if self.stats:
                self.stats.inc_value('scheduler/enqueued/rabbitmq',
                                     spider=self.spider)
            self.queue.push(request.url)
        return True

    def next_request(self):
        """ Creates and returns a request to fire
        """
        if self.closing:
            return

        mframe, hframe, body = self.queue.pop()
        if mframe is not None:
            self.waiting = False

            if self.stats:
                self.stats.inc_value('scheduler/dequeued/rabbitmq',
                                     spider=self.spider)

            request = self.spider._make_request(mframe, hframe, body)
            request.meta['delivery_tag'] = mframe.delivery_tag
            logger.info('Running request {}'.format(request.url))
            return request
        elif body is not None:
            request = self.spider._make_request(None, None, body)
            request.meta['delivery_tag'] = -1
            logger.info('Running request {}'.format(request.url))
            return request
        else:
            if not self.waiting:
                msg = 'Queue {} is empty. Waiting for messages...'
                self.waiting = True
                logger.info(msg.format(self.queue.queue_name))
            time.sleep(10)
            return None

    def has_pending_requests(self):
        return not self.closing


class SaaS(RabbitMQScheduler):
    """ Scheduler as a RabbitMQ service.
    """

    def __init__(self, connection_url, *args, **kwargs):
        super(SaaS, self).__init__(connection_url, *args, **kwargs)

    def ack_message(self, delivery_tag):
        self.queue.ack(delivery_tag)

    def requeue_message(self, body, headers=None):
        self.queue.push(body, headers)
