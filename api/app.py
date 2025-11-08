from fastapi import FastAPI
from web3 import Web3

app = FastAPI()

@app.get("/")
def home():
    return {"status": "success", "message": "Arbitrage Bot API is live on Vercel!"}


# Example: You can test your web3 connection here
@app.get("/network-info")
def network_info():
    try:
        w3 = Web3(Web3.HTTPProvider("https://mainnet.infura.io/v3/YOUR_INFURA_PROJECT_ID"))
        if w3.is_connected():
            return {"connected": True, "network": w3.client_version}
        else:
            return {"connected": False}
    except Exception as e:
        return {"error": str(e)}
