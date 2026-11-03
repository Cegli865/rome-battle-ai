"""Manual console: accept the game hook's connection and send orders by hand.

First live test of the hook, before the AI is plugged in:

    python -m rome_ai.manual

Commands (unit ids are printed when the battle starts; id = alliance<<16 | army<<8 | index,
shown in hex like 01:00:03):
    units                     list units with controller/flags
    take | release            take the AI alliance from the game AI / give it back
    move ID X Y [run]         unit_order_move
    attack ID TARGET [walk]   unit_order_attack_unit
    halt ID                   unit_order_halt
    faw ID on|off             fire at will
    skirm ID on|off           skirmish mode
    form ID TYPE              formation: square horde column wedge testudo phalanx schiltrom shield_wall
"""

from __future__ import annotations

import asyncio
import sys

from .protocol import Attack, BattleEnd, BattleStart, Control, FireAtWill, Formation, Halt, Hello, Log, Move, Orders, Pause, RawState, Resume, Skirmish, State, dump, parse_game_message


MAX_LINE = 16 * 1024 * 1024  # terrain messages from the hook are ~0.5-1 MB on one line


def fmt_id(i: int) -> str:
    return f"{i // 10000 - 1}:{i // 100 % 100}:{i % 100}"


def parse_id(s: str) -> int:
    if ":" in s:
        a, r, u = (int(x) for x in s.split(":"))
        return (a + 1) * 10000 + r * 100 + u
    return int(s, 0)


class Console:
    def __init__(self) -> None:
        self.writer: asyncio.StreamWriter | None = None
        self.units: list = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        print("game connected")
        while line := await reader.readline():
            try:
                msg = parse_game_message(line)
            except Exception as e:
                print("bad message:", e, line[:200])
                continue
            if isinstance(msg, Hello):
                print(f"hello from {msg.game} v{msg.version}")
            elif isinstance(msg, BattleStart):
                print(f"battle started, game AI controls alliance {msg.ai_alliance}")
            elif isinstance(msg, (RawState, State)):
                first = not self.units
                self.units = msg.units
                if first:
                    self.print_units()
            elif isinstance(msg, Log):
                if "unit id=" not in msg.msg:
                    print("[hook]", msg.msg)
            elif isinstance(msg, Pause):
                print("(paused)")
            elif isinstance(msg, Resume):
                print("(resumed)")
            elif isinstance(msg, BattleEnd):
                print("battle ended")
                self.units = []
        print("game disconnected")
        self.writer = None

    def print_units(self) -> None:
        for u in self.units:
            extra = f"controller={u.controller} flags={u.flags:#x}" if hasattr(u, "controller") else f"{u.name} ({u.x:.0f},{u.y:.0f})"
            print(f"  {fmt_id(u.id)}  alliance {u.alliance}  {extra}")

    def send(self, msg) -> None:
        if self.writer is None:
            print("not connected")
            return
        self.writer.write(dump(msg))

    def command(self, line: str) -> None:
        p = line.split()
        if not p:
            return
        c = p[0]
        try:
            if c == "units":
                self.print_units()
            elif c in ("take", "release"):
                self.send(Control(take=c == "take"))
            elif c == "move":
                self.send(Orders(orders=[Move(unit=parse_id(p[1]), x=float(p[2]), y=float(p[3]), run="run" in p[4:])]))
            elif c == "attack":
                self.send(Orders(orders=[Attack(unit=parse_id(p[1]), target=parse_id(p[2]), run="walk" not in p[3:])]))
            elif c == "halt":
                self.send(Orders(orders=[Halt(unit=parse_id(p[1]))]))
            elif c == "form":
                self.send(Orders(orders=[Formation(unit=parse_id(p[1]), formation=p[2])]))
            elif c in ("faw", "skirm"):
                cls = FireAtWill if c == "faw" else Skirmish
                self.send(Orders(orders=[cls(unit=parse_id(p[1]), on=p[2] == "on")]))
            else:
                print(__doc__)
        except (IndexError, ValueError) as e:  # pydantic ValidationError is a ValueError
            print("bad command:", e)


async def main_async(port: int) -> None:
    con = Console()
    server = await asyncio.start_server(con.handle, "127.0.0.1", port, limit=MAX_LINE)
    print(f"listening on 127.0.0.1:{port}; type 'help'")
    loop = asyncio.get_running_loop()
    async with server:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            con.command(line.strip())


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7777
    asyncio.run(main_async(port))


if __name__ == "__main__":
    main()
