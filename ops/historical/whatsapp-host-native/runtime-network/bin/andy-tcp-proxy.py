#!/usr/bin/python3
import asyncio
import sys

lh, lp, th, tp = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])

async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

async def handle(cr, cw):
    try:
        sr, sw = await asyncio.open_connection(th, tp)
    except Exception:
        cw.close()
        return
    await asyncio.gather(pipe(cr, sw), pipe(sr, cw))

async def main():
    server = await asyncio.start_server(handle, lh, lp)
    async with server:
        await server.serve_forever()

asyncio.run(main())
