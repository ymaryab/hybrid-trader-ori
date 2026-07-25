#!/bin/bash
# yaratici-sicil.timer sarmalayicisi (gecelik 04:17 UTC).
# 25 Tem: /tmp'den repoya tasindi (reboot dayanikliligi).
cd /home/bot/yz/hybrid-trader-ori
PYTHONPATH=src /home/bot/yz/gozlem-venv/bin/python \
  -m hibrit_trader.gozlem.yaratici_sicil \
  >> data/yaratici_sicil_calisti.log 2>&1
