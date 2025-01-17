""" Multicast DNS Service Discovery for Python, v0.14-wmcbrine
    Copyright 2003 Paul Scott-Murphy, 2014 William McBrine

    This module provides a framework for the use of DNS Service Discovery
    using IP multicast.

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the Free Software
    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301
    USA
"""

import enum
import socket
import struct
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Tuple, Union, cast

from ._exceptions import AbstractMethodException, IncomingDecodeError, NamePartTooLongException
from ._logger import QuietLogger, log
from ._utils.net import _is_v6_address
from ._utils.struct import int2byte
from ._utils.time import current_time_millis, millis_to_seconds
from .const import (
    _CLASSES,
    _CLASS_MASK,
    _CLASS_UNIQUE,
    _EXPIRE_FULL_TIME_PERCENT,
    _EXPIRE_STALE_TIME_PERCENT,
    _FLAGS_QR_MASK,
    _FLAGS_QR_QUERY,
    _FLAGS_QR_RESPONSE,
    _FLAGS_TC,
    _MAX_MSG_ABSOLUTE,
    _MAX_MSG_TYPICAL,
    _RECENT_TIME_PERCENT,
    _TYPES,
    _TYPE_A,
    _TYPE_AAAA,
    _TYPE_ANY,
    _TYPE_CNAME,
    _TYPE_HINFO,
    _TYPE_PTR,
    _TYPE_SRV,
    _TYPE_TXT,
)


if TYPE_CHECKING:
    # https://github.com/PyCQA/pylint/issues/3525
    from ._cache import DNSCache  # pylint: disable=cyclic-import


class DNSEntry:

    """A DNS entry"""

    def __init__(self, name: str, type_: int, class_: int) -> None:
        self.key = name.lower()
        self.name = name
        self.type = type_
        self.class_ = class_ & _CLASS_MASK
        self.unique = (class_ & _CLASS_UNIQUE) != 0

    def _entry_tuple(self) -> Tuple[str, int, int]:
        """Entry Tuple for DNSEntry."""
        return (self.key, self.type, self.class_)

    def __eq__(self, other: Any) -> bool:
        """Equality test on key (lowercase name), type, and class"""
        return (
            self.key == other.key
            and self.type == other.type
            and self.class_ == other.class_
            and isinstance(other, DNSEntry)
        )

    @staticmethod
    def get_class_(class_: int) -> str:
        """Class accessor"""
        return _CLASSES.get(class_, "?(%s)" % class_)

    @staticmethod
    def get_type(t: int) -> str:
        """Type accessor"""
        return _TYPES.get(t, "?(%s)" % t)

    def entry_to_string(self, hdr: str, other: Optional[Union[bytes, str]]) -> str:
        """String representation with additional information"""
        return "%s[%s,%s%s,%s]%s" % (
            hdr,
            self.get_type(self.type),
            self.get_class_(self.class_),
            "-unique" if self.unique else "",
            self.name,
            "=%s" % cast(Any, other) if other is not None else "",
        )


class DNSQuestion(DNSEntry):

    """A DNS question entry"""

    def answered_by(self, rec: 'DNSRecord') -> bool:
        """Returns true if the question is answered by the record"""
        return (
            self.class_ == rec.class_
            and (self.type == rec.type or self.type == _TYPE_ANY)
            and self.name == rec.name
        )

    @property
    def unicast(self) -> bool:
        """Returns true if the QU (not QM) is set.

        unique shares the same mask as the one
        used for unicast.
        """
        return self.unique

    def __repr__(self) -> str:
        """String representation"""
        return "%s[question,%s,%s,%s]" % (
            self.get_type(self.type),
            "QU" if self.unicast else "QM",
            self.get_class_(self.class_),
            self.name,
        )


class DNSRecord(DNSEntry):

    """A DNS record - like a DNS entry, but has a TTL"""

    # TODO: Switch to just int ttl
    def __init__(self, name: str, type_: int, class_: int, ttl: Union[float, int]) -> None:
        super().__init__(name, type_, class_)
        self.ttl = ttl
        self.created = current_time_millis()
        self._expiration_time = self.get_expiration_time(_EXPIRE_FULL_TIME_PERCENT)
        self._stale_time = self.get_expiration_time(_EXPIRE_STALE_TIME_PERCENT)
        self._recent_time = self.get_expiration_time(_RECENT_TIME_PERCENT)

    def __eq__(self, other: Any) -> bool:  # pylint: disable=no-self-use
        """Abstract method"""
        raise AbstractMethodException

    def suppressed_by(self, msg: 'DNSIncoming') -> bool:
        """Returns true if any answer in a message can suffice for the
        information held in this record."""
        for record in msg.answers:
            if self.suppressed_by_answer(record):
                return True
        return False

    def suppressed_by_answer(self, other: 'DNSRecord') -> bool:
        """Returns true if another record has same name, type and class,
        and if its TTL is at least half of this record's."""
        return self == other and other.ttl > (self.ttl / 2)

    def get_expiration_time(self, percent: int) -> float:
        """Returns the time at which this record will have expired
        by a certain percentage."""
        return self.created + (percent * self.ttl * 10)

    # TODO: Switch to just int here
    def get_remaining_ttl(self, now: float) -> Union[int, float]:
        """Returns the remaining TTL in seconds."""
        return max(0, millis_to_seconds(self._expiration_time - now))

    def is_expired(self, now: float) -> bool:
        """Returns true if this record has expired."""
        return self._expiration_time <= now

    def is_stale(self, now: float) -> bool:
        """Returns true if this record is at least half way expired."""
        return self._stale_time <= now

    def is_recent(self, now: float) -> bool:
        """Returns true if the record more than one quarter of its TTL remaining."""
        return self._recent_time > now

    def reset_ttl(self, other: 'DNSRecord') -> None:
        """Sets this record's TTL and created time to that of
        another record."""
        self.created = other.created
        self.ttl = other.ttl
        self._expiration_time = self.get_expiration_time(_EXPIRE_FULL_TIME_PERCENT)
        self._stale_time = self.get_expiration_time(_EXPIRE_STALE_TIME_PERCENT)
        self._recent_time = self.get_expiration_time(_RECENT_TIME_PERCENT)

    def write(self, out: 'DNSOutgoing') -> None:  # pylint: disable=no-self-use
        """Abstract method"""
        raise AbstractMethodException

    def to_string(self, other: Union[bytes, str]) -> str:
        """String representation with additional information"""
        arg = "%s/%s,%s" % (self.ttl, int(self.get_remaining_ttl(current_time_millis())), cast(Any, other))
        return DNSEntry.entry_to_string(self, "record", arg)


class DNSAddress(DNSRecord):

    """A DNS address record"""

    def __init__(self, name: str, type_: int, class_: int, ttl: int, address: bytes) -> None:
        super().__init__(name, type_, class_, ttl)
        self.address = address

    def write(self, out: 'DNSOutgoing') -> None:
        """Used in constructing an outgoing packet"""
        out.write_string(self.address)

    def __eq__(self, other: Any) -> bool:
        """Tests equality on address"""
        return (
            isinstance(other, DNSAddress) and DNSEntry.__eq__(self, other) and self.address == other.address
        )

    def __hash__(self) -> int:
        """Hash to compare like DNSAddresses."""
        return hash((*self._entry_tuple(), self.address))

    def __repr__(self) -> str:
        """String representation"""
        try:
            return self.to_string(
                socket.inet_ntop(
                    socket.AF_INET6 if _is_v6_address(self.address) else socket.AF_INET, self.address
                )
            )
        except (ValueError, OSError):
            return self.to_string(str(self.address))


class DNSHinfo(DNSRecord):

    """A DNS host information record"""

    def __init__(self, name: str, type_: int, class_: int, ttl: int, cpu: str, os: str) -> None:
        super().__init__(name, type_, class_, ttl)
        self.cpu = cpu
        self.os = os

    def write(self, out: 'DNSOutgoing') -> None:
        """Used in constructing an outgoing packet"""
        out.write_character_string(self.cpu.encode('utf-8'))
        out.write_character_string(self.os.encode('utf-8'))

    def __eq__(self, other: Any) -> bool:
        """Tests equality on cpu and os"""
        return (
            isinstance(other, DNSHinfo)
            and DNSEntry.__eq__(self, other)
            and self.cpu == other.cpu
            and self.os == other.os
        )

    def __hash__(self) -> int:
        """Hash to compare like DNSHinfo."""
        return hash((*self._entry_tuple(), self.cpu, self.os))

    def __repr__(self) -> str:
        """String representation"""
        return self.to_string(self.cpu + " " + self.os)


class DNSPointer(DNSRecord):

    """A DNS pointer record"""

    def __init__(self, name: str, type_: int, class_: int, ttl: int, alias: str) -> None:
        super().__init__(name, type_, class_, ttl)
        self.alias = alias

    def write(self, out: 'DNSOutgoing') -> None:
        """Used in constructing an outgoing packet"""
        out.write_name(self.alias)

    def __eq__(self, other: Any) -> bool:
        """Tests equality on alias"""
        return isinstance(other, DNSPointer) and self.alias == other.alias and DNSEntry.__eq__(self, other)

    def __hash__(self) -> int:
        """Hash to compare like DNSPointer."""
        return hash((*self._entry_tuple(), self.alias))

    def __repr__(self) -> str:
        """String representation"""
        return self.to_string(self.alias)


class DNSText(DNSRecord):

    """A DNS text record"""

    def __init__(self, name: str, type_: int, class_: int, ttl: int, text: bytes) -> None:
        assert isinstance(text, (bytes, type(None)))
        super().__init__(name, type_, class_, ttl)
        self.text = text

    def write(self, out: 'DNSOutgoing') -> None:
        """Used in constructing an outgoing packet"""
        out.write_string(self.text)

    def __hash__(self) -> int:
        """Hash to compare like DNSText."""
        return hash((*self._entry_tuple(), self.text))

    def __eq__(self, other: Any) -> bool:
        """Tests equality on text"""
        return isinstance(other, DNSText) and self.text == other.text and DNSEntry.__eq__(self, other)

    def __repr__(self) -> str:
        """String representation"""
        if len(self.text) > 10:
            return self.to_string(self.text[:7]) + "..."
        return self.to_string(self.text)


class DNSService(DNSRecord):

    """A DNS service record"""

    def __init__(
        self,
        name: str,
        type_: int,
        class_: int,
        ttl: Union[float, int],
        priority: int,
        weight: int,
        port: int,
        server: str,
    ) -> None:
        super().__init__(name, type_, class_, ttl)
        self.priority = priority
        self.weight = weight
        self.port = port
        self.server = server

    def write(self, out: 'DNSOutgoing') -> None:
        """Used in constructing an outgoing packet"""
        out.write_short(self.priority)
        out.write_short(self.weight)
        out.write_short(self.port)
        out.write_name(self.server)

    def __eq__(self, other: Any) -> bool:
        """Tests equality on priority, weight, port and server"""
        return (
            isinstance(other, DNSService)
            and self.priority == other.priority
            and self.weight == other.weight
            and self.port == other.port
            and self.server == other.server
            and DNSEntry.__eq__(self, other)
        )

    def __hash__(self) -> int:
        """Hash to compare like DNSService."""
        return hash((*self._entry_tuple(), self.priority, self.weight, self.port, self.server))

    def __repr__(self) -> str:
        """String representation"""
        return self.to_string("%s:%s" % (self.server, self.port))


class DNSMessage:
    """A base class for DNS messages."""

    def __init__(self, flags: int) -> None:
        """Construct a DNS message."""
        self.flags = flags

    def is_query(self) -> bool:
        """Returns true if this is a query."""
        return (self.flags & _FLAGS_QR_MASK) == _FLAGS_QR_QUERY

    def is_response(self) -> bool:
        """Returns true if this is a response."""
        return (self.flags & _FLAGS_QR_MASK) == _FLAGS_QR_RESPONSE


class DNSIncoming(DNSMessage, QuietLogger):

    """Object representation of an incoming DNS packet"""

    def __init__(self, data: bytes) -> None:
        """Constructor from string holding bytes of packet"""
        super().__init__(0)
        self.offset = 0
        self.data = data
        self.questions = []  # type: List[DNSQuestion]
        self.answers = []  # type: List[DNSRecord]
        self.id = 0
        self.num_questions = 0
        self.num_answers = 0
        self.num_authorities = 0
        self.num_additionals = 0
        self.valid = False

        try:
            self.read_header()
            self.read_questions()
            self.read_others()
            self.valid = True

        except (IndexError, struct.error, IncomingDecodeError):
            self.log_exception_warning('Choked at offset %d while unpacking %r', self.offset, data)

    def __repr__(self) -> str:
        return '<DNSIncoming:{%s}>' % ', '.join(
            [
                'id=%s' % self.id,
                'flags=%s' % self.flags,
                'n_q=%s' % self.num_questions,
                'n_ans=%s' % self.num_answers,
                'n_auth=%s' % self.num_authorities,
                'n_add=%s' % self.num_additionals,
                'questions=%s' % self.questions,
                'answers=%s' % self.answers,
            ]
        )

    def unpack(self, format_: bytes) -> tuple:
        length = struct.calcsize(format_)
        info = struct.unpack(format_, self.data[self.offset : self.offset + length])
        self.offset += length
        return info

    def read_header(self) -> None:
        """Reads header portion of packet"""
        (
            self.id,
            self.flags,
            self.num_questions,
            self.num_answers,
            self.num_authorities,
            self.num_additionals,
        ) = self.unpack(b'!6H')

    def read_questions(self) -> None:
        """Reads questions section of packet"""
        for _ in range(self.num_questions):
            name = self.read_name()
            type_, class_ = self.unpack(b'!HH')

            question = DNSQuestion(name, type_, class_)
            self.questions.append(question)

    # def read_int(self):
    #     """Reads an integer from the packet"""
    #     return self.unpack(b'!I')[0]

    def read_character_string(self) -> bytes:
        """Reads a character string from the packet"""
        length = self.data[self.offset]
        self.offset += 1
        return self.read_string(length)

    def read_string(self, length: int) -> bytes:
        """Reads a string of a given length from the packet"""
        info = self.data[self.offset : self.offset + length]
        self.offset += length
        return info

    def read_unsigned_short(self) -> int:
        """Reads an unsigned short from the packet"""
        return cast(int, self.unpack(b'!H')[0])

    def read_others(self) -> None:
        """Reads the answers, authorities and additionals section of the
        packet"""
        n = self.num_answers + self.num_authorities + self.num_additionals
        for _ in range(n):
            domain = self.read_name()
            type_, class_, ttl, length = self.unpack(b'!HHiH')
            rec = None  # type: Optional[DNSRecord]
            if type_ == _TYPE_A:
                rec = DNSAddress(domain, type_, class_, ttl, self.read_string(4))
            elif type_ in (_TYPE_CNAME, _TYPE_PTR):
                rec = DNSPointer(domain, type_, class_, ttl, self.read_name())
            elif type_ == _TYPE_TXT:
                rec = DNSText(domain, type_, class_, ttl, self.read_string(length))
            elif type_ == _TYPE_SRV:
                rec = DNSService(
                    domain,
                    type_,
                    class_,
                    ttl,
                    self.read_unsigned_short(),
                    self.read_unsigned_short(),
                    self.read_unsigned_short(),
                    self.read_name(),
                )
            elif type_ == _TYPE_HINFO:
                rec = DNSHinfo(
                    domain,
                    type_,
                    class_,
                    ttl,
                    self.read_character_string().decode('utf-8'),
                    self.read_character_string().decode('utf-8'),
                )
            elif type_ == _TYPE_AAAA:
                rec = DNSAddress(domain, type_, class_, ttl, self.read_string(16))
            else:
                # Try to ignore types we don't know about
                # Skip the payload for the resource record so the next
                # records can be parsed correctly
                self.offset += length

            if rec is not None:
                self.answers.append(rec)

    def read_utf(self, offset: int, length: int) -> str:
        """Reads a UTF-8 string of a given length from the packet"""
        return str(self.data[offset : offset + length], 'utf-8', 'replace')

    def read_name(self) -> str:
        """Reads a domain name from the packet"""
        result = ''
        off = self.offset
        next_ = -1
        first = off

        while True:
            length = self.data[off]
            off += 1
            if length == 0:
                break
            t = length & 0xC0
            if t == 0x00:
                result += self.read_utf(off, length) + '.'
                off += length
            elif t == 0xC0:
                if next_ < 0:
                    next_ = off + 1
                off = ((length & 0x3F) << 8) | self.data[off]
                if off >= first:
                    raise IncomingDecodeError("Bad domain name (circular) at %s" % (off,))
                first = off
            else:
                raise IncomingDecodeError("Bad domain name at %s" % (off,))

        if next_ >= 0:
            self.offset = next_
        else:
            self.offset = off

        return result


class DNSOutgoing(DNSMessage):

    """Object representation of an outgoing packet"""

    def __init__(self, flags: int, multicast: bool = True, id_: int = 0) -> None:
        super().__init__(flags)
        self.finished = False
        self.id = id_
        self.multicast = multicast
        self.packets_data: List[bytes] = []

        # these 3 are per-packet -- see also reset_for_next_packet()
        self.names: Dict[str, int] = {}
        self.data: List[bytes] = []
        self.size: int = 12
        self.allow_long: bool = True

        self.state = self.State.init

        self.questions: List[DNSQuestion] = []
        self.answers: List[Tuple[DNSRecord, float]] = []
        self.authorities: List[DNSPointer] = []
        self.additionals: List[DNSRecord] = []

    def reset_for_next_packet(self) -> None:
        self.names = {}
        self.data = []
        self.size = 12
        self.allow_long = True

    def __repr__(self) -> str:
        return '<DNSOutgoing:{%s}>' % ', '.join(
            [
                'multicast=%s' % self.multicast,
                'flags=%s' % self.flags,
                'questions=%s' % self.questions,
                'answers=%s' % self.answers,
                'authorities=%s' % self.authorities,
                'additionals=%s' % self.additionals,
            ]
        )

    class State(enum.Enum):
        init = 0
        finished = 1

    def add_question(self, record: DNSQuestion) -> None:
        """Adds a question"""
        self.questions.append(record)

    def add_answer(self, inp: DNSIncoming, record: DNSRecord) -> None:
        """Adds an answer"""
        if not record.suppressed_by(inp):
            self.add_answer_at_time(record, 0)

    def add_answer_at_time(self, record: Optional[DNSRecord], now: Union[float, int]) -> None:
        """Adds an answer if it does not expire by a certain time"""
        if record is not None and (now == 0 or not record.is_expired(now)):
            self.answers.append((record, now))

    def add_authorative_answer(self, record: DNSPointer) -> None:
        """Adds an authoritative answer"""
        self.authorities.append(record)

    def add_additional_answer(self, record: DNSRecord) -> None:
        """Adds an additional answer

        From: RFC 6763, DNS-Based Service Discovery, February 2013

        12.  DNS Additional Record Generation

           DNS has an efficiency feature whereby a DNS server may place
           additional records in the additional section of the DNS message.
           These additional records are records that the client did not
           explicitly request, but the server has reasonable grounds to expect
           that the client might request them shortly, so including them can
           save the client from having to issue additional queries.

           This section recommends which additional records SHOULD be generated
           to improve network efficiency, for both Unicast and Multicast DNS-SD
           responses.

        12.1.  PTR Records

           When including a DNS-SD Service Instance Enumeration or Selective
           Instance Enumeration (subtype) PTR record in a response packet, the
           server/responder SHOULD include the following additional records:

           o  The SRV record(s) named in the PTR rdata.
           o  The TXT record(s) named in the PTR rdata.
           o  All address records (type "A" and "AAAA") named in the SRV rdata.

        12.2.  SRV Records

           When including an SRV record in a response packet, the
           server/responder SHOULD include the following additional records:

           o  All address records (type "A" and "AAAA") named in the SRV rdata.

        """
        self.additionals.append(record)

    def add_question_or_one_cache(
        self, cache: 'DNSCache', now: float, name: str, type_: int, class_: int
    ) -> None:
        """Add a question if it is not already cached."""
        cached_entry = cache.get_by_details(name, type_, class_)
        if not cached_entry:
            self.add_question(DNSQuestion(name, type_, class_))
        else:
            self.add_answer_at_time(cached_entry, now)

    def add_question_or_all_cache(
        self, cache: 'DNSCache', now: float, name: str, type_: int, class_: int
    ) -> None:
        """Add a question if it is not already cached.
        This is currently only used for IPv6 addresses.
        """
        cached_entries = cache.get_all_by_details(name, type_, class_)
        if not cached_entries:
            self.add_question(DNSQuestion(name, type_, class_))
            return
        for cached_entry in cached_entries:
            self.add_answer_at_time(cached_entry, now)

    def pack(self, format_: Union[bytes, str], value: Any) -> None:
        self.data.append(struct.pack(format_, value))
        self.size += struct.calcsize(format_)

    def write_byte(self, value: int) -> None:
        """Writes a single byte to the packet"""
        self.pack(b'!c', int2byte(value))

    def insert_short_at_start(self, value: int) -> None:
        """Inserts an unsigned short at the start of the packet"""
        self.data.insert(0, struct.pack(b'!H', value))

    def replace_short(self, index: int, value: int) -> None:
        """Replaces an unsigned short in a certain position in the packet"""
        self.data[index] = struct.pack(b'!H', value)

    def write_short(self, value: int) -> None:
        """Writes an unsigned short to the packet"""
        self.pack(b'!H', value)

    def write_int(self, value: Union[float, int]) -> None:
        """Writes an unsigned integer to the packet"""
        self.pack(b'!I', int(value))

    def write_string(self, value: bytes) -> None:
        """Writes a string to the packet"""
        assert isinstance(value, bytes)
        self.data.append(value)
        self.size += len(value)

    def write_utf(self, s: str) -> None:
        """Writes a UTF-8 string of a given length to the packet"""
        utfstr = s.encode('utf-8')
        length = len(utfstr)
        if length > 64:
            raise NamePartTooLongException
        self.write_byte(length)
        self.write_string(utfstr)

    def write_character_string(self, value: bytes) -> None:
        assert isinstance(value, bytes)
        length = len(value)
        if length > 256:
            raise NamePartTooLongException
        self.write_byte(length)
        self.write_string(value)

    def write_name(self, name: str) -> None:
        """
        Write names to packet

        18.14. Name Compression

        When generating Multicast DNS messages, implementations SHOULD use
        name compression wherever possible to compress the names of resource
        records, by replacing some or all of the resource record name with a
        compact two-byte reference to an appearance of that data somewhere
        earlier in the message [RFC1035].
        """

        # split name into each label
        parts = name.split('.')
        if not parts[-1]:
            parts.pop()

        # construct each suffix
        name_suffices = ['.'.join(parts[i:]) for i in range(len(parts))]

        # look for an existing name or suffix
        for count, sub_name in enumerate(name_suffices):
            if sub_name in self.names:
                break
        else:
            count = len(name_suffices)

        # note the new names we are saving into the packet
        name_length = len(name.encode('utf-8'))
        for suffix in name_suffices[:count]:
            self.names[suffix] = self.size + name_length - len(suffix.encode('utf-8')) - 1

        # write the new names out.
        for part in parts[:count]:
            self.write_utf(part)

        # if we wrote part of the name, create a pointer to the rest
        if count != len(name_suffices):
            # Found substring in packet, create pointer
            index = self.names[name_suffices[count]]
            self.write_byte((index >> 8) | 0xC0)
            self.write_byte(index & 0xFF)
        else:
            # this is the end of a name
            self.write_byte(0)

    def write_question(self, question: DNSQuestion) -> bool:
        """Writes a question to the packet"""
        start_data_length, start_size = len(self.data), self.size
        self.write_name(question.name)
        self.write_short(question.type)
        self.write_record_class(question)
        return self._check_data_limit_or_rollback(start_data_length, start_size)

    def write_record_class(self, record: Union[DNSQuestion, DNSRecord]) -> None:
        """Write out the record class including the unique/unicast (QU) bit."""
        if record.unique and self.multicast:
            self.write_short(record.class_ | _CLASS_UNIQUE)
        else:
            self.write_short(record.class_)

    def write_record(self, record: DNSRecord, now: float) -> bool:
        """Writes a record (answer, authoritative answer, additional) to
        the packet.  Returns True on success, or False if we did not (either
        because the packet was already finished or because the record does
        not fit."""
        if self.state == self.State.finished:
            return False

        start_data_length, start_size = len(self.data), self.size
        self.write_name(record.name)
        self.write_short(record.type)
        self.write_record_class(record)
        if now == 0:
            self.write_int(record.ttl)
        else:
            self.write_int(record.get_remaining_ttl(now))
        index = len(self.data)

        self.write_short(0)  # Will get replaced with the actual size
        record.write(self)
        # Adjust size for the short we will write before this record
        length = sum((len(d) for d in self.data[index + 1 :]))
        # Here we replace the 0 length short we wrote
        # before with the actual length
        self.replace_short(index, length)
        return self._check_data_limit_or_rollback(start_data_length, start_size)

    def _check_data_limit_or_rollback(self, start_data_length: int, start_size: int) -> bool:
        """Check data limit, if we go over, then rollback and return False."""
        len_limit = _MAX_MSG_ABSOLUTE if self.allow_long else _MAX_MSG_TYPICAL
        self.allow_long = False

        if self.size <= len_limit:
            return True

        log.debug("Reached data limit (size=%d) > (limit=%d) - rolling back", self.size, len_limit)

        while len(self.data) > start_data_length:
            self.data.pop()
        self.size = start_size

        rollback_names = [name for name, idx in self.names.items() if idx >= start_size]
        for name in rollback_names:
            del self.names[name]
        return False

    def _write_questions_from_offset(self, questions_offset: int) -> int:
        questions_written = 0
        for question in self.questions[questions_offset:]:
            if not self.write_question(question):
                break
            questions_written += 1
        return questions_written

    def _write_answers_from_offset(self, answer_offset: int) -> int:
        answers_written = 0
        for answer, time_ in self.answers[answer_offset:]:
            if not self.write_record(answer, time_):
                break
            answers_written += 1
        return answers_written

    def _write_authorities_from_offset(self, authority_offset: int) -> int:
        authorities_written = 0
        for authority in self.authorities[authority_offset:]:
            if not self.write_record(authority, 0):
                break
            authorities_written += 1
        return authorities_written

    def _write_additionals_from_offset(self, additional_offset: int) -> int:
        additionals_written = 0
        for additional in self.additionals[additional_offset:]:
            if not self.write_record(additional, 0):
                break
            additionals_written += 1
        return additionals_written

    def _has_more_to_add(
        self, questions_offset: int, answer_offset: int, authority_offset: int, additional_offset: int
    ) -> bool:
        """Check if all questions, answers, authority, and additionals have been written to the packet."""
        return (
            questions_offset < len(self.questions)
            or answer_offset < len(self.answers)
            or authority_offset < len(self.authorities)
            or additional_offset < len(self.additionals)
        )

    def packets(self) -> List[bytes]:
        """Returns a list of bytestrings containing the packets' bytes

        No further parts should be added to the packet once this
        is done.  The packets are each restricted to _MAX_MSG_TYPICAL
        or less in length, except for the case of a single answer which
        will be written out to a single oversized packet no more than
        _MAX_MSG_ABSOLUTE in length (and hence will be subject to IP
        fragmentation potentially)."""

        if self.state == self.State.finished:
            return self.packets_data

        questions_offset = 0
        answer_offset = 0
        authority_offset = 0
        additional_offset = 0
        # we have to at least write out the question
        first_time = True

        while first_time or self._has_more_to_add(
            questions_offset, answer_offset, authority_offset, additional_offset
        ):
            first_time = False
            log.debug(
                "offsets = questions=%d, answers=%d, authorities=%d, additionals=%d",
                questions_offset,
                answer_offset,
                authority_offset,
                additional_offset,
            )
            log.debug(
                "lengths = questions=%d, answers=%d, authorities=%d, additionals=%d",
                len(self.questions),
                len(self.answers),
                len(self.authorities),
                len(self.additionals),
            )

            questions_written = self._write_questions_from_offset(questions_offset)
            answers_written = self._write_answers_from_offset(answer_offset)
            authorities_written = self._write_authorities_from_offset(authority_offset)
            additionals_written = self._write_additionals_from_offset(additional_offset)

            self.insert_short_at_start(additionals_written)
            self.insert_short_at_start(authorities_written)
            self.insert_short_at_start(answers_written)
            self.insert_short_at_start(questions_written)

            questions_offset += questions_written
            answer_offset += answers_written
            authority_offset += authorities_written
            additional_offset += additionals_written
            log.debug(
                "now offsets = questions=%d, answers=%d, authorities=%d, additionals=%d",
                questions_offset,
                answer_offset,
                authority_offset,
                additional_offset,
            )

            if self.is_query() and self._has_more_to_add(
                questions_offset, answer_offset, authority_offset, additional_offset
            ):
                # https://datatracker.ietf.org/doc/html/rfc6762#section-7.2
                log.debug("Setting TC flag")
                self.insert_short_at_start(self.flags | _FLAGS_TC)
            else:
                self.insert_short_at_start(self.flags)

            if self.multicast:
                self.insert_short_at_start(0)
            else:
                self.insert_short_at_start(self.id)

            self.packets_data.append(b''.join(self.data))
            self.reset_for_next_packet()

            if (questions_written + answers_written + authorities_written + additionals_written) == 0 and (
                len(self.questions) + len(self.answers) + len(self.authorities) + len(self.additionals)
            ) > 0:
                log.warning("packets() made no progress adding records; returning")
                break
        self.state = self.State.finished
        return self.packets_data
