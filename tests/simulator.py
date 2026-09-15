"""Minimal Modbus TCP inverter for end-to-end HA tests on localhost only."""

import asyncio
import struct


class SimulatedInverter:
    def __init__(self):
        self.registers = {}
        self.requests = []
        self.writes = []
        self.connections = 0
        self.accepted = 0
        self.clients = set()
        self.fail_address = None
        self.server = None
        for address, value in {
            30051: 8009, 30053: 19051, 30057: 123456789,
            33003: 235, 30845: 60, 30849: 220, 40187: 12800,
            31393: 0, 31395: 0, 30775: 3000, 30865: 0,
            30867: 2200, 30773: 1600, 30961: 1600,
        }.items():
            self.set_u32(address, value)

    def set_u32(self, address, value):
        self.registers[address] = (value >> 16) & 65535
        self.registers[address + 1] = value & 65535

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.clients):
            writer.close()
        for writer in list(self.clients):
            await writer.wait_closed()
        await asyncio.sleep(0)

    async def _client(self, reader, writer):
        self.connections += 1
        self.accepted += 1
        self.clients.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                function = pdu[0]
                address, count = struct.unpack(">HH", pdu[1:5])
                self.requests.append((function, address, count))
                if self.fail_address == address:
                    response = bytes([function | 0x80, 2])
                elif function == 3:
                    values = [self.registers.get(address + index, 0) for index in range(count)]
                    response = bytes([3, count * 2]) + struct.pack(f">{count}H", *values)
                elif function == 16:
                    values = struct.unpack(f">{count}H", pdu[6:6 + count * 2])
                    self.writes.append((address, list(values)))
                    self.registers.update({address + index: value for index, value in enumerate(values)})
                    response = bytes([16]) + struct.pack(">HH", address, count)
                else:
                    response = bytes([function | 0x80, 1])
                writer.write(struct.pack(">HHHB", transaction, protocol, len(response) + 1, unit) + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.connections -= 1
            self.clients.discard(writer)
            writer.close()
            await writer.wait_closed()
