from aiohttp import web
import asyncio

async def handle(request):
    print("path:", request.match_info.get('path', ''))
    print("query:", request.query)
    return web.Response(text="ok")

app = web.Application()
app.router.add_get('/miniapp/{path:.*}', handle)

async def test():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8089)
    await site.start()
    
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get('http://localhost:8089/miniapp/need/app.css?v=6') as resp:
            print(await resp.text())
            
    await runner.cleanup()

asyncio.run(test())
