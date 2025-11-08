export PRIVATE_KEY="0x3338dfda1fc285a5a312443877589c48df5300e1971a7a375105969601221841" && \
export INFURA_KEY="96b52d8457dd4a8494b4f985a331a3c1" && \
export TATUM_KEY="t-6859e1d47b2cac50cedeba0a-ed669e7df8fb45b5a993d267" && \
export WEB3_PROVIDER_URI="https://sepolia.infura.io/v3/$INFURA_KEY" && \
/usr/local/bin/python3 arbitrage_executer.py exec \
  --path 0xfff9976782d46cc05630d1f6ebab18b2324d6b14,0x779877A7B0D9E8603169DdbD7836e478b4624789,0xfff9976782d46cc05630d1f6ebab18b2324d6b14 \
  --amount-in 0.005 \
  --network sepolia
