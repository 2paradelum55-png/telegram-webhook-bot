from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    print(update)
    return {"ok": True}
