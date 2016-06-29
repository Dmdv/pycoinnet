import asyncio
import binascii
import logging
import struct

from pycoin import encoding


class ProtocolError(Exception):
    pass


class Peer:
    DEFAULT_MAX_MSG_SIZE = 2*1024*1024

    def __init__(self, stream_reader, stream_writer, magic_header,
                 parse_from_data_f, pack_from_data_f, max_msg_size=DEFAULT_MAX_MSG_SIZE):
        self._reader = stream_reader
        self._writer = stream_writer
        self._magic_header = magic_header
        self._parse_from_data = parse_from_data_f
        self._pack_from_data = pack_from_data_f
        self._max_msg_size = max_msg_size
        self._msg_lock = asyncio.Lock()
        # stats
        self._bytes_read = 0
        self._bytes_writ = 0

    def send_msg(self, message_name, **kwargs):
        message_data = self._pack_from_data(message_name, **kwargs)
        message_type = message_name.encode("utf8")
        message_type_padded = (message_type+(b'\0'*12))[:12]
        message_size = struct.pack("<L", len(message_data))
        message_checksum = encoding.double_sha256(message_data)[:4]
        packet = b"".join([
            self._magic_header, message_type_padded, message_size, message_checksum, message_data
        ])
        logging.debug("sending message %s [%d bytes] to %s", message_type.decode("utf8"), len(packet), self)
        self._bytes_writ += len(packet)
        self._writer.write(packet)

    @asyncio.coroutine
    def next_message(self, unpack_to_dict=True):
        header_size = len(self._magic_header)
        with (yield from self._msg_lock):
            # read magic header
            reader = self._reader
            blob = yield from reader.readexactly(header_size)
            self._bytes_read += header_size
            if blob != self._magic_header:
                raise ProtocolError("bad magic: got %s" % binascii.hexlify(blob))

            # read message name
            message_size_hash_bytes = yield from reader.readexactly(20)
            self._bytes_read += 20
            message_name_bytes = message_size_hash_bytes[:12]
            message_name = message_name_bytes.replace(b"\0", b"").decode("utf8")

            # get size of message
            size_bytes = message_size_hash_bytes[12:16]
            size = int.from_bytes(size_bytes, byteorder="little")
            if size > self._max_msg_size:
                raise ProtocolError("absurdly large message size %d" % size)

            # read the hash, then the message
            transmitted_hash = message_size_hash_bytes[16:20]
            message_data = yield from reader.readexactly(size)
            self._bytes_read += size

        # check the hash
        actual_hash = encoding.double_sha256(message_data)[:4]
        if actual_hash != transmitted_hash:
            raise ProtocolError("checksum is WRONG: %s instead of %s" % (
                binascii.hexlify(actual_hash), binascii.hexlify(transmitted_hash)))
        logging.debug("message %s: %s (%d byte payload)", self, message_name, len(message_data))
        if unpack_to_dict:
            message_data = self._parse_from_data(message_name, message_data)
        return message_name, message_data

    def close(self):
        self._writer.close()

    def __repr__(self):
        return "<Peer %s>" % str(self._reader._transport)