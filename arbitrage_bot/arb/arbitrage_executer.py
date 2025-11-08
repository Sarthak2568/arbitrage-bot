#!/usr/bin/env python3
"""
arbitrage_executer.py – exec / scan utility for Uniswap-V2 style arbitrage

CLI
---
# pair-scanner (unchanged)
python arbitrage_executer.py scan

# execute a single swap loop on Sepolia
python arbitrage_executer.py exec \
  --path 0xfff9976782d46cc05630d1f6ebab18b2324d6b14,0x779877A7B0D9E8603169DdbD7836e478b4624789,0xfff9976782d46cc05630d1f6ebab18b2324d6b14 \
  --amount-in 0.005 \
  --network sepolia
"""
import os, sys, time, json, argparse, requests
from decimal import Decimal
from pathlib import Path
from web3 import Web3
from eth_account import Account
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

# -------------------------------------------------------------------- #
#                       --- STATIC CONFIG ---                          #
# -------------------------------------------------------------------- #

DEFAULT_INFURA_KEY = "96b52d8457dd4a8494b4f985a331a3c1"
DEFAULT_TATUM_KEY  = "t-6859e1d47b2cac50cedeba0a-ed669e7df8fb45b5a993d267"

NETWORKS = {
    "sepolia": {
        "chain_id" : 11155111,
        "rpc"      : f"https://sepolia.infura.io/v3/{os.getenv('INFURA_KEY', DEFAULT_INFURA_KEY)}",
        "router"   : Web3.to_checksum_address("0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3"),
        "factory"  : Web3.to_checksum_address("0xF62c03E08ada871A0bEb309762E260a7a6a880E6")
    },
    "mainnet": {
        "chain_id" : 1,
        "rpc"      : f"https://mainnet.infura.io/v3/{os.getenv('INFURA_KEY', DEFAULT_INFURA_KEY)}",
        "router"   : Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"),
        "factory"  : Web3.to_checksum_address("0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f")
    },
}

UNISWAP_ROUTER_ABI = [
    {
        "name":"swapExactTokensForTokens",
        "type":"function",
        "inputs":[
            {"name":"amountIn","type":"uint256"},
            {"name":"amountOutMin","type":"uint256"},
            {"name":"path","type":"address[]"},
            {"name":"to","type":"address"},
            {"name":"deadline","type":"uint256"}],
        "outputs":[{"name":"amounts","type":"uint256[]"}],
        "stateMutability":"nonpayable"
    }
]

ERC20_ABI = [
    {"constant":True,"type":"function","name":"decimals","inputs":[],"outputs":[{"type":"uint8"}]},
    {"constant":True,"type":"function","name":"symbol"  ,"inputs":[],"outputs":[{"type":"string"}]},
    {"constant":True,"type":"function","name":"allowance","inputs":[{"type":"address"},{"type":"address"}],"outputs":[{"type":"uint256"}]},
    {"constant":False,"type":"function","name":"approve" ,"inputs":[{"type":"address"},{"type":"uint256"}],"outputs":[{"type":"bool"}]},
]

# -------------------------------------------------------------------- #
#                       --- HELPERS / UTILS ---                        #
# -------------------------------------------------------------------- #

def get_w3(network: str) -> Web3:
    url = os.getenv("WEB3_PROVIDER_URI", NETWORKS[network]["rpc"])
    w3  = Web3(Web3.HTTPProvider(url))
    if not w3.is_connected():
        sys.exit(f"[FATAL] cannot connect to RPC {url}")
    return w3

def current_gas(w3: Web3, tip_gwei: int = 2) -> dict:
    base = w3.eth.get_block("latest")["baseFeePerGas"]
    tip  = w3.to_wei(tip_gwei, "gwei")
    return {
        "maxFeePerGas": base + tip,
        "maxPriorityFeePerGas": tip
    }

def ensure_allowance(token_addr, owner, spender, amount, w3, gas_params, chain_id):
    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    try:
        if token.functions.allowance(owner, spender).call() >= amount:
            return None
    except (BadFunctionCallOutput, ContractLogicError):
        raise RuntimeError(f"{token_addr} isn't an ERC-20 on this network")

    nonce = w3.eth.get_transaction_count(owner)
    tx = token.functions.approve(spender, Web3.to_int(2**256-1)).build_transaction({
        "from": owner,
        "nonce": nonce,
        "gas": 70_000,
        "chainId": chain_id,
        **gas_params
    })
    return w3.eth.account.sign_transaction(tx, private_key=os.environ["PRIVATE_KEY"]).rawTransaction

def broadcast(raw_tx: bytes, w3: Web3):
    try:
        return w3.eth.send_raw_transaction(raw_tx).hex()
    except Exception as e:
        print(f"[WARN] direct send failed → trying Tatum: {e}")
        tatum = os.getenv("TATUM_KEY", DEFAULT_TATUM_KEY)
        r = requests.post(
            "https://api-eu1.tatum.io/v3/ethereum/broadcast",
            headers={"x-api-key": tatum,"Content-Type":"application/json"},
            json={"txData": raw_tx.hex()}
        )
        if r.status_code != 200:
            raise RuntimeError(f"Tatum broadcast failed: {r.text}")
        return r.json()["txId"]

def build_swap(router, amount_in, path, to, gas_params, chain_id, nonce):
    deadline = int(time.time()) + 900
    tx = router.functions.swapExactTokensForTokens(
        amount_in, 1, path, to, deadline
    ).build_transaction({
        "from": to,
        "nonce": nonce,
        "gas": 300_000,
        "chainId": chain_id,
        **gas_params
    })
    return tx

# -------------------------------------------------------------------- #
#                          --- EXEC MODE ---                           #
# -------------------------------------------------------------------- #

def exec_arbitrage(args):
    w3  = get_w3(args.network)
    me  = Account.from_key(os.environ["PRIVATE_KEY"]).address
    cfg = NETWORKS[args.network]
    gas = current_gas(w3)
    path = [Web3.to_checksum_address(x) for x in args.path.split(",")]

    amount_in = w3.to_wei(Decimal(args.amount_in), "ether")  # assumes 18-dec token0

    # 1) Allowance if first token isn't native ETH sentinel
    first_token = path[0]
    raw = ensure_allowance(
        token_addr=first_token,
        owner=me,
        spender=cfg["router"],
        amount=amount_in,
        w3=w3,
        gas_params=gas,
        chain_id=cfg["chain_id"],
    )
    if raw:
        print("[INFO] sending approval tx …")
        tx_hash = broadcast(raw, w3)
        print(f"[INFO] approval hash → {tx_hash}")
        time.sleep(15)

    # 2) Build & sign swap
    router = w3.eth.contract(address=cfg["router"], abi=UNISWAP_ROUTER_ABI)
    nonce  = w3.eth.get_transaction_count(me)
    swap_tx = build_swap(router, amount_in, path, me, gas, cfg["chain_id"], nonce)
    signed  = w3.eth.account.sign_transaction(swap_tx, private_key=os.environ["PRIVATE_KEY"])
    final_hash = broadcast(signed.rawTransaction, w3)
    print(f"[✅] swap tx → {final_hash}")

# -------------------------------------------------------------------- #
#                       --- SCANNER (unchanged) ---                    #
# -------------------------------------------------------------------- #

ABIS_FILE = Path(__file__).with_name("abis.txt")
BATCH_SIZE, SLEEP_BETWEEN_BATCHES = 500, 1
RATE_LIMIT_SLEEP, FULL_SCAN_INTERVAL = 60, 3600

def load_seen_pairs():  # unchanged helper
    seen = set()
    if ABIS_FILE.exists():
        for line in ABIS_FILE.read_text().splitlines():
            try:
                seen.add(json.loads(line)["pairAddress"].lower())
            except Exception:
                pass
    return seen

def append_record(rec):  # unchanged helper
    with ABIS_FILE.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")

def fetch_all_pairs():   # minimal version (omit details here if not needed)
    # Dummy stub: keep your full implementation or import from old file
    print("[INFO] scan stub – implement or import your original fetch_all_pairs()")

# -------------------------------------------------------------------- #
#                            --- CLI ---                               #
# -------------------------------------------------------------------- #

def cli():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    e = sub.add_parser("exec")
    e.add_argument("--path", required=True)
    e.add_argument("--amount-in", required=True, type=Decimal)
    e.add_argument("--network", default="sepolia", choices=NETWORKS.keys())
    return p.parse_args()

if __name__ == "__main__":
    if "PRIVATE_KEY" not in os.environ:
        sys.exit("Set PRIVATE_KEY env-var first")

    args = cli()
    if args.cmd == "scan":
        fetch_all_pairs()
    else:
        exec_arbitrage(args)