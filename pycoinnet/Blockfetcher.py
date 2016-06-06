
import asyncio
import logging
import weakref

from pycoinnet.msg.InvItem import InvItem, ITEM_TYPE_BLOCK


class Blockfetcher:
    """
    Blockfetcher

    This class parallelizes block fetching.
    When a new peer is connected, pass it in to add_peer
    and forward all messages of type "block" to handle_msg.

    To download a list of blocks, call "fetch_blocks".

    It accepts new peers via add_peer.

    It fetches new blocks via get_block_future or get_block.
    """
    def __init__(self):
        # this queue accepts tuples of the form:
        #  (priority, InvItem(ITEM_TYPE_BLOCK, block_hash), future, peers_tried)
        self._block_hash_priority_queue = asyncio.PriorityQueue()
        self._get_batch_lock = asyncio.Lock()
        self._futures = weakref.WeakValueDictionary()

    def fetch_blocks(self, block_hash_priority_pair_list):
        """
        block_hash_priority_pair_list is a list of
        tuples with (block_hash, priority).
        The priority is generally expected block index.
        Blocks are prioritized by this priority.

        Returns: a list of futures, each corresponding to a tuple.
        """
        r = []
        for bh, pri in block_hash_priority_pair_list:
            f = asyncio.Future()
            peers_tried = set()
            item = (pri, bh, f, peers_tried)
            self._block_hash_priority_queue.put_nowait(item)
            f.item = item
            r.append(f)
            self._futures[bh] = f
        return r

    def add_peer(self, peer):
        """
        Register a new peer, and start the loop which polls it for blocks.
        """
        asyncio.get_event_loop().create_task(self._fetcher_loop(peer))

    def handle_msg(self, name, data):
        """
        When a peer gets a block message, it should invoked this method.
        """
        if name == 'block':
            block = data.get("block")
            bh = block.hash()
            f = self._futures.get(bh)
            if f and not f.done():
                f.set_result(block)

    @asyncio.coroutine
    def _get_batch(self, batch_size, peer):
        logging.info("getting batch up to size %d for %s", batch_size, peer)
        with (yield from self._get_batch_lock):
            skipped = []
            inv_items = []
            futures = []
            while len(futures) < batch_size:
                if self._block_hash_priority_queue.empty() and len(futures) > 0:
                    break
                item = yield from self._block_hash_priority_queue.get()
                (pri, block_hash, block_future, peers_tried) = item
                if block_future.done():
                    continue
                if peer in peers_tried:
                    skipped.append(item)
                    continue
                peers_tried.add(peer)
                inv_items.append(InvItem(ITEM_TYPE_BLOCK, block_hash))
                futures.append(block_future)
            for item in skipped:
                self._block_hash_priority_queue.put_nowait(item)
        start_batch_time = asyncio.get_event_loop().time()
        try:
            peer.send_msg("getdata", items=inv_items)
        except Exception:
            logging.exception("problem sending getdata msg to %s", peer)
            for f in futures:
                self._block_hash_priority_queue.put_nowait(f.item)
        logging.info("returning batch of size %d for %s", len(futures), peer)
        logging.debug("requesting %s from %s", [f.item[0] for f in futures], peer)
        return futures, start_batch_time

    @asyncio.coroutine
    def _fetcher_loop(self, peer, target_batch_time=3, max_batch_time=6):
        MAX_BATCH_SIZE = 500
        initial_batch_size = 10
        batch_size = initial_batch_size
        loop = asyncio.get_event_loop()
        batch_1, start_batch_time_1 = yield from self._get_batch(batch_size=batch_size, peer=peer)
        try:
            while True:
                batch_2, start_batch_time_2 = yield from self._get_batch(batch_size=batch_size, peer=peer)
                yield from asyncio.wait(batch_1, timeout=max_batch_time)
                # look for futures that need to be retried
                item_count = 0
                for f in batch_1:
                    if not f.done():
                        self._block_hash_priority_queue.put_nowait(f.item)
                        logging.error("timeout waiting for block %d, requing", f.item[0])
                    else:
                        item_count += 1
                # calculate new batch size
                batch_time = loop.time() - start_batch_time_1
                logging.info("got batch size %d in time %s", item_count, batch_time)
                if item_count == 0:
                    item_count = 1
                time_per_item = batch_time / item_count
                batch_size = min(int(target_batch_time / time_per_item) + 1, MAX_BATCH_SIZE)
                batch_1 = batch_2
                logging.info("new batch size is %d", batch_size)
                start_batch_time_1 = start_batch_time_2
        except EOFError:
            logging.info("peer %s disconnected", peer)
        except Exception:
            logging.exception("problem with peer %s", peer)