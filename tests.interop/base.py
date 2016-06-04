
import asyncio
import os
import unittest

from pycoin.serialize import h2b_rev

from pycoinnet.InvFetcher import InvFetcher
from pycoinnet.PeerProtocol import PeerProtocol
from pycoinnet.msg.InvItem import InvItem, ITEM_TYPE_BLOCK, ITEM_TYPE_MERKLEBLOCK, ITEM_TYPE_TX
from pycoinnet.msg.PeerAddress import PeerAddress
from pycoinnet.networks import MAINNET


def run(f):
    return asyncio.get_event_loop().run_until_complete(f)


VERSION_MSG = dict(
    version=70001, subversion=b"/Notoshi/", services=1, timestamp=1392760610,
    remote_address=PeerAddress(1, "127.0.0.2", 6111),
    local_address=PeerAddress(1, "127.0.0.1", 6111),
    nonce=3412075413544046060,
    last_block_index=10000
)


class InteropTest(unittest.TestCase):
    def setUp(self):
        try:
            host_port = os.getenv("BITCOIND_HOSTPORT")
            self.host, self.port = host_port.split(":")
            self.port = int(self.port)
        except Exception:
            raise ValueError('need to set BITCOIND_HOSTPORT="127.0.0.1:8333" for example')

    def test_connect(self):
        loop = asyncio.get_event_loop()
        transport, protocol = run(loop.create_connection(
            lambda: PeerProtocol(MAINNET), host=self.host, port=self.port))
        protocol.send_msg("version", **VERSION_MSG)
        msg = run(protocol.next_message())
        assert msg[0] == 'version'
        protocol.send_msg("verack")
        msg = run(protocol.next_message())
        assert msg[0] == 'verack'
        protocol.send_msg("mempool")
        msg_name, msg_data = run(protocol.next_message())
        if msg_name == 'inv':
            items = msg_data.get("items")
            protocol.send_msg("getdata", items=items)
            for _ in range(len(items)):
                msg_name, msg_data = run(protocol.next_message())
                print(msg_data.get("tx"))

    def test_InvFetcher(self):
        BLOCK_95150_HASH = h2b_rev("00000000000026ace69f5cbe46f7bbe868737635edef3354ef09fdaad8c755fb")
        loop = asyncio.get_event_loop()
        transport, protocol = run(loop.create_connection(
            lambda: PeerProtocol(MAINNET), host=self.host, port=self.port))
        inv_fetcher = InvFetcher(protocol)
        dispatcher = Dispatcher(protocol)
        dispatcher.add_method(inv_fetcher.handle_msg)
        version_data = run(dispatcher.handshake())
        print(version_data)
        asyncio.get_event_loop().create_task(dispatcher.dispatch_messages())
        inv_item = InvItem(ITEM_TYPE_BLOCK, BLOCK_95150_HASH)
        bl = run(inv_fetcher.fetch(inv_item))
        assert len(bl.txs) == 5

        inv_item = InvItem(ITEM_TYPE_MERKLEBLOCK, BLOCK_95150_HASH)
        mb = run(inv_fetcher.fetch(inv_item))
        txs = [run(f) for f in mb.tx_futures]
        assert len(txs) == 5
        for tx1, tx2 in zip(txs, bl.txs):
            assert tx1.id() == tx2.id()

        # test "notfound"
        inv_item = InvItem(ITEM_TYPE_TX, h2b_rev("f"*64))
        b = run(inv_fetcher.fetch(inv_item))
        assert b is None

    def test_headers_catchup(self):
        future = asyncio.Future()

        def handle_msg(peer, name, data):
            print(name)
            if name != 'headers':
                return
            if future.done():
                return
            headers = [bh for bh, t in data["headers"]]
            return future.set_result(headers)

        loop = asyncio.get_event_loop()
        transport, protocol = run(loop.create_connection(
            lambda: PeerProtocol(MAINNET), host=self.host, port=self.port))
        dispatcher = Dispatcher(protocol)
        dispatcher.add_method(handle_msg)
        version_data = run(dispatcher.handshake())
        print(version_data)
        asyncio.get_event_loop().create_task(dispatcher.dispatch_messages())
        hash_stop = b'\0' * 32
        block_locator_hashes = [hash_stop]
        protocol.send_msg(message_name="getheaders",
                          version=1, hashes=block_locator_hashes, hash_stop=hash_stop)
        headers = run(future)
        print(headers)


class Dispatcher:
    def __init__(self, peer):
        self._methods = dict()
        self._method_id = 0
        self._peer = peer

    def add_method(self, method):
        id = self._method_id
        self._methods[id] = method
        self._method_id += 1
        return id

    def remove_method(self, id):
        if id in self._methods:
            del self._methods

    def handle_msg(self, name, data):
        loop = asyncio.get_event_loop()
        for m in self._methods.values():
            # each method gets its own copy of the data dict
            # to protect from it being changed
            data = dict(data)
            if asyncio.iscoroutinefunction(m):
                loop.create_task(m(self._peer, name, data))
            else:
                loop.call_soon(m, self._peer, name, data)

    @asyncio.coroutine
    def handshake(self):
        # "version"
        self._peer.send_msg("version", **VERSION_MSG)
        msg, version_data = yield from self._peer.next_message()
        self.handle_msg(msg, version_data)
        assert msg == 'version'

        # "verack"
        self._peer.send_msg("verack")
        msg, verack_data = yield from self._peer.next_message()
        self.handle_msg(msg, verack_data)
        assert msg == 'verack'
        return version_data

    @asyncio.coroutine
    def dispatch_messages(self):
        # loop
        while True:
            msg, data = yield from self._peer.next_message()
            self.handle_msg(msg, data)