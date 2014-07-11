from collections import defaultdict
from functools import partial
import json
import logging
import os
import socket
import time

from hash_ring import HashRing

from kafka.client import KafkaClient
from kafka.consumer import SimpleConsumer

from kazoo.client import KazooClient
from kazoo.exceptions import KazooException
from kazoo.exceptions import NodeExistsError
from kazoo.exceptions import NoNodeError
from kazoo.protocol.states import Callback
from kazoo.protocol.states import KazooState
from kazoo.retry import KazooRetry
from kazoo.retry import RetryFailedError
from kazoo.recipe.watchers import PatientChildrenWatch
from kazoo.recipe.partitioner import PartitionState


# https://cwiki.apache.org/confluence/display/KAFKA/Kafka+data+structures+in+Zookeeper
class ZKPartitioner(object):
    """Claims partitions in a Kafka topic.

    When the :class:`ZKPartitioner` enters the
    :attr:`~PartitionState.FAILURE` state, it is unrecoverable
    and a new :class:`ZKPartitioner` should be created.

    Example:

    .. code-block:: python

        from kazoo.client import KazooClient
        client = KazooClient()

        qp = ZKPartitioner(client, consumer_group, topic)

        while 1:
            if qp.failed:
                raise Exception("Lost or unable to acquire partition")
            elif qp.release:
                qp.release_set()
            elif qp.acquired:
                for partition in qp:
                    # Do something with each partition
            elif qp.allocating:
                qp.wait_for_acquire()

    **State Transitions**

    When created, the :class:`ZKPartitioner` enters the
    :attr:`PartitionState.ALLOCATING` state.

    :attr:`~PartitionState.ALLOCATING` ->
    :attr:`~PartitionState.ACQUIRED`

        Set was partitioned successfully, the partition list assigned
        is accessible via list/iter methods or calling list() on the
        :class:`ZKPartitioner` instance.

    :attr:`~PartitionState.ALLOCATING` ->
    :attr:`~PartitionState.FAILURE`

        Allocating the set failed either due to a Zookeeper session
        expiration, or failure to acquire the items of the set within
        the timeout period.

    :attr:`~PartitionState.ACQUIRED` ->
    :attr:`~PartitionState.RELEASE`

        The members of the party have changed, and the set needs to be
        repartitioned. :meth:`ZKPartitioner.release` should be called
        as soon as possible.

    :attr:`~PartitionState.ACQUIRED` ->
    :attr:`~PartitionState.FAILURE`

        The current partition was lost due to a Zookeeper session
        expiration.

    :attr:`~PartitionState.RELEASE` ->
    :attr:`~PartitionState.ALLOCATING`

        The current partition was released and is being re-allocated.

    """

    path_formats = {
        'owner': '/consumers/{group}/owners/{topic}/{partition}',
        'party': '/consumers/{group}/ids',
        'topic': '/brokers/topics/{topic}/partitions',
    }

    def __init__(self, client, group, topic,
                 identifier=None, time_boundary=30):
        """Create a :class:`~ZKPartitioner` instance

        :param client: A :class:`~kazoo.client.KazooClient` instance.
        :param identifier: An identifier to use for this member of the
                           party when participating. Defaults to the
                           hostname + process id.
        :param time_boundary: How long the party members must be stable
                              before allocation can complete.

        """
        self.logger = logging.getLogger('kafka.consumer.zkpartitioner')
        self.state = PartitionState.ALLOCATING
        self.group = group
        self.topic = topic
        self.consumer_partitions = defaultdict(list)

        self._locks = []
        self._client = client
        self._identifier = identifier or '%s-%s' % (
            socket.getfqdn(), os.getpid()
        )
        self._party_path = self.path_formats['party'].format(group=group)

        self._set = []
        self._client.handler.spawn(self.get_partitions)

        self._time_boundary = time_boundary

        self._acquire_event = client.handler.event_object()

        self.consumer_ring = None
        # Join the party
        self.join_party()

        self._was_allocated = False
        self._state_change = client.handler.rlock_object()
        client.add_listener(self._establish_sessionwatch)

        # Now watch the party and set the callback on the async result
        # so we know when we're ready
        self._children_updated = False
        self._child_watching(self._allocate_transition, async=True)

    def get_partitions(self):
        t_partition_path = self.path_formats['topic'].format(topic=self.topic)
        self._set = self._client.get_children(t_partition_path)

    def __iter__(self):
        """Return the partitions in this partition set"""
        for partition in self.consumer_partitions[self._identifier]:
            yield partition

    @property
    def failed(self):
        """Corresponds to the :attr:`PartitionState.FAILURE` state"""
        return self.state == PartitionState.FAILURE

    @property
    def release(self):
        """Corresponds to the :attr:`PartitionState.RELEASE` state"""
        return self.state == PartitionState.RELEASE

    @property
    def allocating(self):
        """Corresponds to the :attr:`PartitionState.ALLOCATING`
        state"""
        return self.state == PartitionState.ALLOCATING

    @property
    def acquired(self):
        """Corresponds to the :attr:`PartitionState.ACQUIRED` state"""
        return self.state == PartitionState.ACQUIRED

    def join_party(self):
        self._party = self._client.ShallowParty(self._party_path,
                                                identifier=self._identifier)
        # Mimic Scala data:
        # {"version":1,"subscription":{"foo.bar":1},"pattern":"static","timestamp":"1404263054520"}
        subscription = {
            'version': 1,
            'subscription': {self.topic: 1},
            'pattern': 'static',
            'timestamp': int(time.time() * 1000),
        }
        self._party.data = json.dumps(subscription)
        self.logger.info('Joining party %s as %s',
                         self._party_path, self._identifier)
        self._party.join()

    def wait_for_acquire(self, timeout=30):
        """Wait for the set to be partitioned and acquired

        :param timeout: How long to wait before returning.
        :type timeout: int

        """
        self._acquire_event.wait(timeout)

    def release_set(self):
        """Call to release the set

        This method begins the step of allocating once the set has
        been released.

        """
        self._release_locks()
        if self._locks:  # pragma: nocover
            # This shouldn't happen, it means we couldn't release our
            # locks, abort
            self._fail_out()
            return
        else:
            with self._state_change:
                if self.failed:
                    return
                self.state = PartitionState.ALLOCATING
        self._child_watching(self._allocate_transition, async=True)

    def finish(self):
        """Call to release the set and leave the party"""
        self._release_locks()
        self._fail_out()

    def _fail_out(self):
        with self._state_change:
            self.state = PartitionState.FAILURE
        if self._party.participating:
            try:
                self.logger.warn('Leaving party: %s ( %s )',
                                 self._party_path, self._identifier)
                self._party.leave()
            except KazooException:  # pragma: nocover
                pass

    def _allocate_transition(self, result):
        """Called when in allocating mode, and the children settled"""
        # Did we get an exception waiting for children to settle?
        if result.exception:  # pragma: nocover
            self._fail_out()
            return

        children, async_result = result.get()
        self._children_updated = False

        # Add a callback when children change on the async_result
        def updated(result):
            with self._state_change:
                if self.acquired:
                    self.state = PartitionState.RELEASE
            self._children_updated = True

        async_result.rawlink(updated)

        # Proceed to acquire locks for the working set as needed
        self.consumer_ring = HashRing(list(self._party))
        self.rebalance(self._set)

        # All locks acquired! Time for state transition, make sure
        # we didn't inadvertently get lost thus far
        with self._state_change:
            if self.failed:  # pragma: nocover
                return self.finish()
            self.state = PartitionState.ACQUIRED
            self.logger.info('All partitions acquired: %d',
                             len(self.consumer_partitions[self._identifier]))
            self._acquire_event.set()

    def _release_locks(self):
        """Attempt to completely remove all the locks"""
        self._acquire_event.clear()

        my_partitions = self.consumer_partitions[self._identifier]
        my_old_partitions = [
            partition
            for partition in self._set
            if self.consumer_ring.get_node(partition) != self._identifier and
                int(partition) in my_partitions
        ]
        for partition in my_old_partitions:
            self.logger.info('Releasing ownership of partition %s',
                             partition)
            p_path = self.path_formats['owner'].format(group=self.group,
                                                       topic=self.topic,
                                                       partition=partition)
            try:
                self.zk.delete(p_path)
                my_partitions.remove(int(partition))
            except NoNodeError as err:
                msg = "Couldn't release partition ownership for: %s"
                self.logger.exception(msg, partition)

    def _abort_lock_acquisition(self):
        """Called during lock acquisition if a party change occurs"""
        self._partition_set = []
        self._release_locks()
        if self._locks:
            # This shouldn't happen, it means we couldn't release our
            # locks, abort
            self._fail_out()
            return
        return self._child_watching(self._allocate_transition)

    def _child_watching(self, func=None, async=False):
        """Called when children are being watched to stabilize

        This actually returns immediately, child watcher spins up a
        new thread/greenlet and waits for it to stabilize before
        any callbacks might run.

        """
        watcher = PatientChildrenWatch(self._client, self._party_path,
                                       self._time_boundary)
        asy = watcher.start()
        if func is not None:
            # We spin up the function in a separate thread/greenlet
            # to ensure that the rawlink's it might use won't be
            # blocked
            if async:
                func = partial(self._client.handler.spawn, func)
            asy.rawlink(func)
        return asy

    def _establish_sessionwatch(self, state):
        """Register ourself to listen for session events, we shut down
        if we become lost"""
        with self._state_change:
            # Handle network partition: If connection gets suspended,
            # change state to ALLOCATING if we had already ACQUIRED. This way
            # the caller does not process the members since we could eventually
            # lose session get repartitioned. If we got connected after a suspension
            # it means we've not lost the session and still have our members. Hence,
            # restore to ACQUIRED
            if state == KazooState.SUSPENDED:
                if self.state == PartitionState.ACQUIRED:
                    self._was_allocated = True
                    self.state = PartitionState.ALLOCATING
            elif state == KazooState.CONNECTED:
                if self._was_allocated:
                    self._was_allocated = False
                    self.state = PartitionState.ACQUIRED

        if state == KazooState.LOST:
            self._client.handler.spawn(self._fail_out)
            return True

    def _partitioner(self, identifier, members, partitions):
        self.consumer_ring = HashRing(members)
        my_partitions = []
        for partition in partitions:
            member = self.consumer_ring.get_node(partition)
            if member == identifier:
                my_partitions.append(int(partition))

        return sorted(my_partitions)

    def rebalance(self, partition_ids):
        kr = KazooRetry(max_tries=3)
        kr.retry_exceptions = kr.retry_exceptions + tuple([NodeExistsError])

        my_partitions = self.consumer_partitions[self._identifier]

        # Clean up old ownership data first, so we don't block
        # the joining node(s)
        self._release_locks()

        my_new_partitions = [
            partition
            for partition in partition_ids
            if self.consumer_ring.get_node(partition) == self._identifier and \
               int(partition) not in my_partitions
        ]
        for partition in my_new_partitions:
            c_id = self.consumer_ring.get_node(partition)
            self.consumer_partitions[c_id].append(int(partition))
            p_path = self.path_formats['owner'].format(group=self.group,
                                                       topic=self.topic,
                                                       partition=partition)
            try:
                self.logger.info('Acquiring ownership of partition %s',
                                 partition)
                kr(self._client.create, p_path,
                   value=self._identifier, ephemeral=True, makepath=True)
            except RetryFailedError as err:
                # A different consumer had been registered as the owner
                expired_cid, zstat = self._client.get(p_path)
                msg = 'Acquiring ownership of partition %s (was owned by %s)'
                self.logger.warn(msg, partition, expired_cid)
                # We need to delete / create, so that the node is created
                # ephemeral and owned by us
                self._client.delete(p_path)
                self._client.create(p_path, value=self.zkp._identifier,
                                    ephemeral=True, makepath=True)


class ZKConsumer(object):

    zk_timeout = 30
    jitter_seconds = 5
    broker_prefix = '/brokers/ids'

    def __init__(
            self,
            zk_hosts,
            group,
            topic,
            zk_handler=None,
            **consumer_kwargs):
        """Creates a Consumer that tracks state in ZooKeeper,
        rebalancing partition ownership as registered consumers change.
        NOTE: this class is intended for version 0.8.1 of Kafka, where offsets
              are managed by Kafka but there is no rebalancing in the protocol.
        """
        self.logger = logging.getLogger('kafka.consumer.ZKConsumer')

        self.zk_handler = zk_handler
        self.zk_hosts = zk_hosts

        self.group = group
        self.topic = topic

        self.zk = None
        self.client = None
        self.consumer = None
        self.consumer_kwargs = consumer_kwargs

        # This will kick off a cascading sequence to initialize ourselves:
        # 1. Connect to ZK and pull list of Kafka brokers
        # 2. Register ourselves as a consumer in ZK
        # 3. Rebalance partitions across all connected consumers
        self.init_zk()

    def zk_session_watch(self, state):
        self.logger.info('ZK transitioned to: %s', state)
        #if state == 'CONNECTED' and \
        #   self.zkp is not None and \
        #   self.zkp.failed:
        #    self.logger.info('Restarting ZK partitioner')
        #    self.zk.handler.spawn(self.init_zkp)

    def init_zkp(self):
        self.zkp = ZKPartitioner(self.zk, self.group, self.topic,
                                 time_boundary=self.jitter_seconds)
        # Setup watchers
        # NOTE: the order here is important, as kazoo will call them initially
        #       (as well as when they change):
        #       1. Setup Kafka client connection
        #       2. Setup consistent hashing ring of connected consumers
        #       3. Rebalance partition ownership across all consumers
        @self.zk.ChildrenWatch(self.broker_prefix)
        def broker_change_proxy(broker_ids):
            self.onBrokerChange(broker_ids)

        while 1:
            if self.zkp.failed:
                raise Exception("Lost or unable to acquire partition")
            elif self.zkp.release:
                self.zkp.release_set()
            elif self.zkp.acquired:
                self.init_consumer(list(self.zkp))
                @self.zk.ChildrenWatch(self.zkp._party_path)
                def consumer_change_proxy(consumer_ids):
                    self.onConsumerChange(consumer_ids)

                t_path = self.zkp.path_formats['topic'].format(topic=self.topic)
                @self.zk.ChildrenWatch(t_path)
                def topic_change_proxy(partition_ids):
                    self.onTopicChange(partition_ids)

                break
            elif self.zkp.allocating:
                self.zkp.wait_for_acquire()

    def init_zk(self):
        # TODO: switch to async
        # 1. implement kazoo.interfaces.IHandler in terms of Tornado's IOLoop
        self.zk = KazooClient(hosts=self.zk_hosts, handler=self.zk_handler)
        self.zk.add_listener(self.zk_session_watch)
        self.zk.start()
        self.init_zkp()

    def onBrokerChange(self, broker_ids):
        broker_hosts = []
        for b_id in broker_ids:
            b_json, zstat = self.zk.get('/'.join([self.broker_prefix, b_id]))
            b_data = json.loads(b_json)
            broker_hosts.append('%s:%d' % (b_data['host'], b_data['port']))

        if self.client is not None:
            self.client.close()

        self.client = KafkaClient(broker_hosts, client_id=self.zkp._identifier)

    def onTopicChange(self, partition_ids):
        callback = Callback('rebalance',
                            self.zkp.rebalance,
                            (partition_ids,))
        self.zk.handler.dispatch_callback(callback)

    def onConsumerChange(self, consumer_ids):
        self.consumer_ring = HashRing(consumer_ids)
        t_path = self.zkp.path_formats['topic'].format(topic=self.topic)
        topic_partitions = self.zk.get_children(t_path)
        callback = Callback('rebalance',
                            self.zkp.rebalance,
                            (topic_partitions,))
        self.zk.handler.dispatch_callback(callback)

    def init_consumer(self, my_partitions):
        if self.consumer is not None:
            if my_partitions != sorted(self.consumer.offsets.keys()):
                self.consumer.stop()
            else:
                return

        self.consumer = SimpleConsumer(self.client, self.group, self.topic,
                                       partitions=my_partitions,
                                       **self.consumer_kwargs)
        self.logger.info("Connected to Kafka: %s", self.consumer.offsets)

    def stop(self):
        if self.consumer is not None:
            self.logger.info('Stopping Kafka consumer')
            self.consumer.stop()
        if self.client is not None:
            self.logger.info('Stopping Kafka client')
            self.client.close()
        if self.zk is not None:
            self.logger.info('Stopping ZooKeeper client')
            self.zkp.finish()
            self.zk.stop()

    def commit(self, partitions=None):
        """
        Commit offsets for this consumer

        partitions: list of partitions to commit, default is to commit
                    all of them
        """
        self.logger.info('Begin committing offsets for partitions: %s',
                         partitions if partitions else 'All')
        self.consumer.commit(partitions)
        self.logger.info('End committing offsets for partitions: %s',
                         partitions if partitions else 'All')

    def pending(self, partitions=None):
        """
        Gets the pending message count

        partitions: list of partitions to check for, default is to check all
        """
        return self.consumer.pending(partitions)

    def provide_partition_info(self):
        """
        Indicates that partition info must be returned by the consumer
        """
        self.consumer.provide_partition_info()

    def seek(self, offset, whence):
        """
        Alter the current offset in the consumer, similar to fseek

        offset: how much to modify the offset
        whence: where to modify it from
                0 is relative to the earliest available offset (head)
                1 is relative to the current offset
                2 is relative to the latest known offset (tail)
        """
        self.consumer.seek(offset, whence)

    def get_messages(self, count=1, block=True, timeout=0.1):
        """
        Fetch the specified number of messages

        count: Indicates the maximum number of messages to be fetched
        block: If True, the API will block till some messages are fetched.
        timeout: If block is True, the function will block for the specified
                 time (in seconds) until count messages is fetched. If None,
                 it will block forever.
        """
        return self.consumer.get_messages(count, block, timeout)

    def get_message(self, block=True, timeout=0.1, get_partition_info=None):
        return self.consumer.get_message(block, timeout, get_partition_info)

    def _get_message(self, block=True, timeout=0.1, get_partition_info=None,
                     update_offset=True):
        return self.consumer._get_message(block, timeout, get_partition_info,
                                          update_offset)

    def __iter__(self):
        for msg in self.consumer:
            yield msg
