"""
Package for interacting on the network at a high level.
"""
import pickle
import httplib
import nacl.signing, nacl.hash, nacl.encoding

from binascii import hexlify

from seed import peers

from twisted.internet.task import LoopingCall
from twisted.internet import defer, reactor, task

from dht.log import Logger
from dht.protocol import KademliaProtocol
from dht.utils import deferredDict, digest
from dht.storage import ForgetfulStorage
from dht.node import Node
from dht.crawling import ValueSpiderCrawl
from dht.crawling import NodeSpiderCrawl
from dht import kprotocol

class Server(object):
    """
    High level view of a node instance.  This is the object that should be created
    to start listening as an active node on the network.
    """

    def __init__(self, node, ksize=20, alpha=3, storage=None):
        """
        Create a server instance.  This will start listening on the given port.

        Args:
            node: The node instance for this peer. It must contain (at minimum) an ID,
                public key, ip address, and port.
            ksize (int): The k parameter from the paper
            alpha (int): The alpha parameter from the paper
            storage: An instance that implements :interface:`~dht.storage.IStorage`
        """
        self.ksize = ksize
        self.alpha = alpha
        self.log = Logger(system=self)
        self.storage = storage or ForgetfulStorage()
        self.node = node
        self.protocol = KademliaProtocol(self.node, self.storage, ksize)
        self.refreshLoop = LoopingCall(self.refreshTable).start(3600)

    def listen(self, port):
        """
        Start listening on the given port.

        This is the same as calling::

            reactor.listenUDP(port, server.protocol)
        """
        return reactor.listenUDP(port, self.protocol)

    def refreshTable(self):
        """
        Refresh buckets that haven't had any lookups in the last hour
        (per section 2.3 of the paper).
        """
        ds = []
        for id in self.protocol.getRefreshIDs():
            node = Node(id)
            nearest = self.protocol.router.findNeighbors(node, self.alpha)
            spider = NodeSpiderCrawl(self.protocol, node, nearest)
            ds.append(spider.find())

        def republishKeys(_):
            ds = []
            # Republish keys older than one hour
            for keyword in self.storage.iterkeys():
                for k, v in self.storage.iteritems(keyword):
                    if self.storage[keyword].get_ttl(k) < 601200:
                        ds.append(self.set(keyword, k, v))

        return defer.gatherResults(ds).addCallback(republishKeys)

    def querySeed(self, seed, pubkey):
        """
        Query an HTTP seed and return a `list` if (ip, port) `tuple` pairs.

        Args:
           seed: A `string` consisting of "ip:port" or "hostname:port"
           pubkey: The hex encoded public key to verify the signature on the response
        """
        nodes = []
        c = httplib.HTTPConnection(seed)
        c.request("GET", "/")
        response = c.getresponse()
        self.log.info("Https response from %s: %s, %s" % (seed, response.status, response.reason))
        data = response.read()
        reread_data = data.decode("zlib")
        seeds = peers.PeerSeeds()
        try:
            seeds.ParseFromString(reread_data)
            for peer in seeds.peer_data:
                p = peers.PeerData()
                p.ParseFromString(peer)
                tup = (str(p.ip_address), p.port)
                nodes.append(tup)
            verify_key = nacl.signing.VerifyKey(pubkey, encoder=nacl.encoding.HexEncoder)
            verify_key.verify(seed.signature + "".join(seeds.peer_data))
        except:
            self.log.error("Error parsing seed response.")
        return nodes

    def bootstrappableNeighbors(self):
        """
        Get a :class:`list` of (ip, port) :class:`tuple` pairs suitable for use as an argument
        to the bootstrap method.

        The server should have been bootstrapped
        already - this is just a utility for getting some neighbors and then
        storing them if this server is going down for a while.  When it comes
        back up, the list of nodes can be used to bootstrap.
        """
        neighbors = self.protocol.router.findNeighbors(self.node)
        return [tuple(n)[-2:] for n in neighbors]

    def bootstrap(self, addrs):
        """
        Bootstrap the server by connecting to other known nodes in the network.

        Args:
            addrs: A `list` of (ip, port) `tuple` pairs.  Note that only IP addresses
                   are acceptable - hostnames will cause an error.
        """

        # if the transport hasn't been initialized yet, wait a second
        if self.protocol.transport is None:
            return task.deferLater(reactor, 1, self.bootstrap, addrs)

        def initTable(results):
            nodes = []
            for addr, result in results.items():
                if result[0]:
                    n = kprotocol.Node()
                    try:
                        n.ParseFromString(result[1][0])
                        pubkey = n.signedPublicKey[len(n.signedPublicKey) - 32:]
                        verify_key = nacl.signing.VerifyKey(pubkey)
                        verify_key.verify(n.signedPublicKey)
                        h = nacl.hash.sha512(n.signedPublicKey)
                        pow = h[64:128]
                        if int(pow[:6], 16) >= 50 or hexlify(n.guid) != h[:40]:
                            raise Exception('Invalid GUID')
                        nodes.append(Node(n.guid, addr[0], addr[1], n.signedPublicKey))
                    except:
                        self.log.msg("Bootstrap node returned invalid GUID")
            spider = NodeSpiderCrawl(self.protocol, self.node, nodes, self.ksize, self.alpha)
            return spider.find()

        ds = {}
        for addr in addrs:
            ds[addr] = self.protocol.ping((addr[0], addr[1]))
        return deferredDict(ds).addCallback(initTable)

    def inetVisibleIP(self):
        """
        Get the internet visible IP's of this node as other nodes see it.

        Returns:
            A `list` of IP's.  If no one can be contacted, then the `list` will be empty.
        """

        def handle(results):
            ips = []
            for result in results:
                if result[0]:
                    ips.append((result[1][0], int(result[1][1])))
            self.log.debug("other nodes think our ip is %s" % str(ips))
            return ips

        ds = []
        for neighbor in self.bootstrappableNeighbors():
            ds.append(self.protocol.stun(neighbor))
        return defer.gatherResults(ds).addCallback(handle)

    def get(self, keyword):
        """
        Get a key if the network has it.

        Returns:
            :class:`None` if not found, the value otherwise.
        """
        node = Node(digest(keyword))
        nearest = self.protocol.router.findNeighbors(node)
        if len(nearest) == 0:
            self.log.warning("There are no known neighbors to get key %s" % keyword)
            return defer.succeed(None)
        spider = ValueSpiderCrawl(self.protocol, node, nearest, self.ksize, self.alpha)
        return spider.find()

    def set(self, keyword, key, value):
        """
        Set the given key/value tuple at the hash of the given keyword.
        All values stored in the DHT are stored as dictionaries of key/value
        pairs. If a value already exists for a given keyword, the new key/value
        pair will be appended to the dictionary.

        Args:
            keyword: a `string` keyword. The SHA1 hash of which will be used as
                the key when inserting in the DHT.
            key: the 20 byte hash of the contract.
            value: a serialized `kprotocol.Node` object with all optional fields
                provided.

        Return: True if at least one peer responded. False if the store rpc
            completely failed.
        """
        self.log.debug("setting '%s' = '%s':'%s' on network" % (keyword, hexlify(key), hexlify(value)))
        dkey = digest(keyword)

        def store(nodes):
            self.log.info("setting '%s' on %s" % (keyword, map(str, nodes)))
            ds = [self.protocol.callStore(node, dkey, key, value) for node in nodes]

            keynode = Node(keyword)
            ownBucket = self.protocol.router.buckets[self.protocol.router.getBucketFor(self.node)]
            if ownBucket.hasInRange(keynode):
                self.log.debug("got a store request from %s, storing value" % str(self.node))
                self.storage[keyword] = (key, value)

            return defer.DeferredList(ds).addCallback(self._anyRespondSuccess)

        node = Node(dkey)
        nearest = self.protocol.router.findNeighbors(node)
        if len(nearest) == 0:
            self.log.warning("There are no known neighbors to set key %s" % key)
            return defer.succeed(False)
        spider = NodeSpiderCrawl(self.protocol, node, nearest, self.ksize, self.alpha)
        return spider.find().addCallback(store)

    def delete(self, keyword, key, signature):
        """
        Delete the given key/value pair from the keyword dictionary on the network.
        To delete you must provide a signature covering the key that you wish to
        delete. It will be verified against the public key stored in the value. We
        use our ksize as alpha to make sure we reach as many nodes storing our value
        as possible.

        Args:
            keyword: the `string` keyword where the data being deleted is stored.
            key: the 20 byte hash of the contract.
            signature: a signature covering the key.

        """
        self.log.debug("deleting '%s':'%s' from the network" % (keyword, hexlify(key)))
        dkey = digest(keyword)

        def delete(nodes):
            self.log.info("deleting '%s' on %s" % (key, map(str, nodes)))
            ds = [self.protocol.callDelete(node, dkey, key, signature) for node in nodes]

            if self.storage.getSpecific(keyword, key) is not None:
                self.storage.delete(keyword, key)

            return defer.DeferredList(ds).addCallback(self._anyRespondSuccess)

        node = Node(dkey)
        nearest = self.protocol.router.findNeighbors(node)
        if len(nearest) == 0:
            self.log.warning("There are no known neighbors to delete key %s" % key)
            return defer.succeed(False)
        spider = NodeSpiderCrawl(self.protocol, node, nearest, self.ksize, self.ksize)
        return spider.find().addCallback(delete)

    def _anyRespondSuccess(self, responses):
        """
        Given the result of a DeferredList of calls to peers, ensure that at least
        one of them was contacted and responded with a Truthy result.
        """
        for deferSuccess, result in responses:
            peerReached, peerResponse = result
            if deferSuccess and peerReached and peerResponse:
                return True
        return False

    def saveState(self, fname):
        """
        Save the state of this node (the alpha/ksize/id/immediate neighbors)
        to a cache file with the given fname.
        """
        data = {'ksize': self.ksize,
                'alpha': self.alpha,
                'id': self.node.id,
                'signed_pubkey': self.node.signed_pubkey,
                'neighbors': self.bootstrappableNeighbors()}
        if len(data['neighbors']) == 0:
            self.log.warning("No known neighbors, so not writing to cache.")
            return
        with open(fname, 'w') as f:
            pickle.dump(data, f)

    @classmethod
    def loadState(self, fname, ip_address, port, storage=None):
        """
        Load the state of this node (the alpha/ksize/id/immediate neighbors)
        from a cache file with the given fname.
        """
        with open(fname, 'r') as f:
            data = pickle.load(f)
        n = Node(data['id'], ip_address, port, data['signed_pubkey'])
        s = Server(n, data['ksize'], data['alpha'], storage=storage)
        if len(data['neighbors']) > 0:
            s.bootstrap(data['neighbors'])
        return s

    def saveStateRegularly(self, fname, frequency=600):
        """
        Save the state of node with a given regularity to the given
        filename.

        Args:
            fname: File name to save retularly to
            frequencey: Frequency in seconds that the state should be saved.
                        By default, 10 minutes.
        """
        loop = LoopingCall(self.saveState, fname)
        loop.start(frequency)
        return loop
