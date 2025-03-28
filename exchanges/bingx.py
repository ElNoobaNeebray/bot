import asyncio
import traceback
import os
import json
import numpy as np

from uuid import uuid4
from njit_funcs import calc_diff
from passivbot import Bot, logging
from procedures import print_async_exception, utc_ms, make_get_filepath
from pure_funcs import determine_pos_side_ccxt, floatify, calc_hash, ts_to_date_utc

import ccxt.async_support as ccxt

assert (
    ccxt.__version__ == "4.0.57"
), f"Currently ccxt {ccxt.__version__} is installed. Please pip reinstall requirements.txt or install ccxt v4.0.57 manually"


class BingXBot(Bot):
    def __init__(self, config: dict):
        self.exchange = "bingx"
        self.market_type = config["market_type"] = "linear_perpetual"
        self.inverse = config["inverse"] = False

        self.max_n_orders_per_batch = 7
        self.max_n_cancellations_per_batch = 10

        super().__init__(config)
        self.cc = getattr(ccxt, "bingx")(
            {
                "apiKey": self.key,
                "secret": self.secret,
                #"headers": {"referer": self.broker_code} if self.broker_code else {},
            }
        )

    def init_market_type(self):
        if not self.symbol.endswith("USDT"):
            raise Exception(f"unsupported symbol {self.symbol}")

    async def fetch_market_info_from_cache(self):
        fname = make_get_filepath(f"caches/bingx_market_info.json")
        info = None
        try:
            if os.path.exists(fname):
                info = json.load(open(fname))
                logging.info("loaded market info from cache")
            if info is None or utc_ms() - info["dump_ts"] > 1000 * 60 * 60 * 24:
                info = {"info": await self.cc.fetch_markets(), "dump_ts": utc_ms()}
                json.dump(info, open(fname, "w"))
                logging.info("dumped market info to cache")
        except Exception as e:
            logging.error(f"failed to load market info from cache {e}")
            traceback.print_exc()
            print_async_exception(info)
            if info is None:
                info = {"info": await self.cc.fetch_markets(), "dump_ts": utc_ms()}
        return info["info"]

    async def _init(self):
        info = await self.fetch_market_info_from_cache()
        self.symbol_id = self.symbol
        for elm in info:
            if elm["baseId"] + elm["quoteId"] == self.symbol_id and elm["type"] == "swap":
                break
        else:
            raise Exception(f"unsupported symbol {self.symbol}")
        self.symbol_id = elm['id']
        self.symbol = elm["symbol"]
        self.max_leverage = elm["limits"]["leverage"]["max"]
        self.coin = elm["base"]
        self.quote = elm["quote"]
        self.price_step = self.config["price_step"] = round(1.0 / (10 ** elm["precision"]["price"]), 12)
        self.qty_step = self.config["qty_step"] = round(1.0 / (10 ** elm["precision"]["amount"]), 12)
        self.min_qty = self.config["min_qty"] = elm["contractSize"]
        self.min_cost = self.config["min_cost"] = (
            2.0 if elm["limits"]["cost"]["min"] is None else elm["limits"]["cost"]["min"]
        )
        self.margin_coin = self.quote
        await super()._init()

    async def fetch_ticker(self, symbol=None):
        fetched = None
        try:
            fetched = await self.cc.swap_v2_public_get_quote_depth(params={'symbol': self.symbol_id, 'limit': 5})
            ticker = {'bid': sorted(floatify(fetched['data']['bids']))[-1][0], 'ask': sorted(floatify(fetched['data']['asks']))[0][0]}
            ticker['last'] = np.random.choice([ticker['bid'], ticker['ask']])
            return ticker
        except Exception as e:
            logging.error(f"error fetching ticker {e}")
            print_async_exception(fetched)
            return None

    async def init_order_book(self):
        return await self.update_ticker()

    async def fetch_open_orders(self) -> [dict]:
        open_orders = None
        try:
            open_orders = await self.cc.fetch_open_orders(symbol=self.symbol, limit=50)
            if len(open_orders) == 50:
                # fetch more
                pass
            return [
                {
                    "order_id": e["id"],
                    "custom_id": e["clientOrderId"],
                    "symbol": e["symbol"],
                    "price": e["price"],
                    "qty": e["amount"],
                    "type": e["type"],
                    "side": e["side"],
                    "position_side": determine_pos_side_ccxt(e),
                    "timestamp": e["timestamp"],
                }
                for e in open_orders
            ]
        except Exception as e:
            logging.error(f"error fetching open orders {e}")
            print_async_exception(open_orders)
            traceback.print_exc()
            return False

    async def transfer_from_derivatives_to_spot(self, coin: str, amount: float):
        return

    async def get_server_time(self):
        server_time = None
        try:
            server_time = await self.cc.fetch_time()
            return server_time
        except Exception as e:
            logging.error(f"error fetching server time {e}")
            print_async_exception(server_time)
            traceback.print_exc()

    async def fetch_position(self) -> dict:
        positions, balance = None, None
        try:
            positions, balance = await asyncio.gather(
                self.cc.fetch_positions(self.symbol), self.cc.fetch_balance()
            )
            positions = [e for e in positions if e["symbol"] == self.symbol]
            position = {
                "long": {"size": 0.0, "price": 0.0, "liquidation_price": 0.0},
                "short": {"size": 0.0, "price": 0.0, "liquidation_price": 0.0},
                "wallet_balance": 0.0,
                "equity": 0.0,
            }
            if positions:
                for p in positions:
                    if p["side"] == "long":
                        position["long"] = {
                            "size": p["contracts"],
                            "price": 0.0 if p["entryPrice"] is None else p["entryPrice"],
                            "liquidation_price": p["liquidationPrice"]
                            if p["liquidationPrice"]
                            else 0.0,
                        }
                    elif p["side"] == "short":
                        position["short"] = {
                            "size": -abs(p["contracts"]),
                            "price": 0.0 if p["entryPrice"] is None else p["entryPrice"],
                            "liquidation_price": p["liquidationPrice"]
                            if p["liquidationPrice"]
                            else 0.0,
                        }
            position["wallet_balance"] = balance[self.quote]["total"]
            return position
        except Exception as e:
            logging.error(f"error fetching pos or balance {e}")
            print_async_exception(positions)
            print_async_exception(balance)
            traceback.print_exc()
        return

        positions, balance = None, None
        try:
            positions, balance = await asyncio.gather(
                self.cc.fetch_positions(),
                self.cc.fetch_balance(),
            )
            positions = [e for e in positions if e["symbol"] == self.symbol]
            position = {
                "long": {"size": 0.0, "price": 0.0, "liquidation_price": 0.0},
                "short": {"size": 0.0, "price": 0.0, "liquidation_price": 0.0},
                "wallet_balance": 0.0,
                "equity": 0.0,
            }
            if positions:
                for p in positions:
                    if p["side"] == "long":
                        position["long"] = {
                            "size": p["contracts"],
                            "price": p["entryPrice"],
                            "liquidation_price": p["liquidationPrice"]
                            if p["liquidationPrice"]
                            else 0.0,
                        }
                    elif p["side"] == "short":
                        position["short"] = {
                            "size": p["contracts"],
                            "price": p["entryPrice"],
                            "liquidation_price": p["liquidationPrice"]
                            if p["liquidationPrice"]
                            else 0.0,
                        }
            if balance:
                for elm in balance["info"]["data"]:
                    for elm2 in elm["details"]:
                        if elm2["ccy"] == self.quote:
                            position["wallet_balance"] = float(elm2["cashBal"])
                            break
            return position
        except Exception as e:
            logging.error(f"error fetching pos or balance {e}")
            print_async_exception(positions)
            print_async_exception(balance)
            traceback.print_exc()

    async def execute_orders(self, orders: [dict]) -> [dict]:
        return await self.execute_multiple(
            orders, self.execute_order, "creations", self.max_n_orders_per_batch
        )

    async def execute_order(self, order: dict) -> dict:
        executed = None
        try:
            executed = await self.cc.create_limit_order(
                symbol=order["symbol"] if "symbol" in order else self.symbol,
                side=order["side"],
                amount=abs(order["qty"]),
                price=order["price"],
                params={
                    "positionIdx": 1 if order["position_side"] == "long" else 2,
                    "timeInForce": "postOnly",
                    "orderLinkId": order["custom_id"] + str(uuid4()),
                },
            )
            if "symbol" not in executed or executed["symbol"] is None:
                executed["symbol"] = order["symbol"] if "symbol" in order else self.symbol
            for key in ["side", "position_side", "qty", "price"]:
                if key not in executed or executed[key] is None:
                    executed[key] = order[key]
            return executed
        except Exception as e:
            logging.error(f"error executing order {order} {e}")
            print_async_exception(executed)
            traceback.print_exc()
            return {}

    async def execute_multiple(self, orders: [dict], func, type_: str, max_n_executions: int):
        if not orders:
            return []
        executions = []
        for order in sorted(orders, key=lambda x: calc_diff(x["price"], self.price))[
            :max_n_executions
        ]:
            execution = None
            try:
                execution = asyncio.create_task(func(order))
                executions.append((order, execution))
            except Exception as e:
                logging.error(f"error executing {type_} {order} {e}")
                print_async_exception(execution)
                traceback.print_exc()
        results = []
        for execution in executions:
            result = None
            try:
                result = await execution[1]
                results.append(result)
            except Exception as e:
                logging.error(f"error executing {type_} {execution} {e}")
                print_async_exception(result)
                traceback.print_exc()
        return results

    async def execute_cancellations(self, orders: [dict]) -> [dict]:
        if len(orders) > self.max_n_cancellations_per_batch:
            # prioritize cancelling reduce-only orders
            try:
                reduce_only_orders = [x for x in orders if x["reduce_only"]]
                rest = [x for x in orders if not x["reduce_only"]]
                orders = (reduce_only_orders + rest)[:max_n_cancellations_per_batch]
            except Exception as e:
                logging.error("debug filter cancellations {e}")
        return await self.execute_multiple(
            orders, self.execute_cancellation, "cancellations", self.max_n_cancellations_per_batch
        )

    async def execute_cancellation(self, order: dict) -> dict:
        executed = None
        try:
            executed = await self.cc.cancel_derivatives_order(order["order_id"], symbol=self.symbol)
            return {
                "symbol": executed["symbol"],
                "side": order["side"],
                "order_id": executed["id"],
                "position_side": order["position_side"],
                "qty": order["qty"],
                "price": order["price"],
            }
        except Exception as e:
            logging.error(f"error cancelling order {order} {e}")
            print_async_exception(executed)
            traceback.print_exc()
            return {}

    async def fetch_account(self):
        return

    async def fetch_ticks(self, from_id: int = None, do_print: bool = True):
        return

    async def fetch_ohlcvs(
        self, symbol: str = None, start_time: int = None, interval="1m", limit=1000
    ):
        ohlcvs = None
        # m -> minutes; h -> hours; d -> days; w -> weeks; M -> months
        interval_map = {
            "1m": 1,
            "3m": 3,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1h": 60,
            "2h": 120,
            "4h": 240,
            "6h": 360,
            "12h": 720,
            "1d": "D",
            "1w": "W",
            "1M": "M",
        }
        assert interval in interval_map, f"unsupported timeframe {interval}"
        try:
            ohlcvs = await self.cc.fetch_ohlcv(
                self.symbol if symbol is None else symbol,
                timeframe=interval_map[interval],
                limit=limit,
                params={} if start_time is None else {"startTime": int(start_time)},
            )
            keys = ["timestamp", "open", "high", "low", "close", "volume"]
            return [{k: elm[i] for i, k in enumerate(keys)} for elm in ohlcvs]
        except Exception as e:
            logging.error(f"error fetching ohlcv {e}")
            print_async_exception(ohlcvs)
            traceback.print_exc()

    async def get_all_income(
        self,
        symbol: str = None,
        start_time: int = None,
        income_type: str = "Trade",
        end_time: int = None,
    ):
        return await self.fetch_income(symbol=symbol, start_time=start_time, end_time=end_time)

    async def transfer_from_derivatives_to_spot(self, coin: str, amount: float):
        transferred = None
        try:
            transferred = await self.cc.transfer(coin, amount, "CONTRACT", "SPOT")
            return transferred
        except:
            logging.error(f"error transferring from derivatives to spot {e}")
            print_async_exception(transferred)
            traceback.print_exc()

    async def fetch_income(
        self,
        symbol: str = None,
        start_time: int = None,
        end_time: int = None,
    ):
        fetched = None
        incomed = {}
        try:
            limit = 100
            params = {"category": "linear", "limit": limit}
            if symbol is not None:
                params["symbol"] = symbol
            if end_time is not None:
                params["endTime"] = int(end_time)
            fetched = await self.cc.private_get_v5_position_closed_pnl(params)
            fetched["result"]["list"] = floatify(fetched["result"]["list"])
            while True:
                if fetched["result"]["list"] == []:
                    break
                for elm in fetched["result"]["list"]:
                    incomed[calc_hash(elm)] = elm
                if start_time is None:
                    break
                if fetched["result"]["list"][-1]["updatedTime"] <= start_time:
                    break
                params["cursor"] = fetched["result"]["nextPageCursor"]
                fetched = await self.cc.private_get_v5_position_closed_pnl(params)
                fetched["result"]["list"] = floatify(fetched["result"]["list"])
                logging.debug(
                    f"fetching income {ts_to_date_utc(fetched['result']['list'][-1]['updatedTime'])}"
                )
            return [
                {
                    "symbol": elm["symbol"],
                    "income": elm["closedPnl"],
                    "token": "USDT",
                    "timestamp": elm["updatedTime"],
                    "info": elm,
                    "transaction_id": elm["orderId"],
                    "trade_id": elm["orderId"],
                }
                for elm in sorted(incomed.values(), key=lambda x: x["updatedTime"])
            ]
            return sorted(incomed.values(), key=lambda x: x["updatedTime"])
        except Exception as e:
            logging.error(f"error fetching income {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return []

    async def fetch_latest_fills(self):
        fetched = None
        try:
            fetched = await self.cc.fetch_my_trades(symbol=self.symbol)
            fills = [
                {
                    "order_id": elm["id"],
                    "symbol": elm["symbol"],
                    "custom_id": elm["info"]["orderLinkId"],
                    "price": elm["price"],
                    "qty": elm["amount"],
                    "type": elm["type"],
                    "reduce_only": None,
                    "side": elm["side"].lower(),
                    "position_side": determine_pos_side_ccxt(elm),
                    "timestamp": elm["timestamp"],
                }
                for elm in fetched
                if elm["amount"] != 0.0 and elm["type"] is not None
            ]
            return sorted(fills, key=lambda x: x["timestamp"])
        except Exception as e:
            logging.error(f"error fetching latest fills {e}")
            print_async_exception(fetched)
            traceback.print_exc()

    async def fetch_fills(
        self,
        limit: int = 200,
        from_id: int = None,
        start_time: int = None,
        end_time: int = None,
    ):
        return []

    async def init_exchange_config(self):
        return
        try:
            res = await self.cc.set_derivatives_margin_mode(
                marginMode="cross", symbol=self.symbol, params={"leverage": self.leverage}
            )
            logging.info(f"cross mode set {res}")
        except Exception as e:
            logging.error(f"error setting cross mode: {e}")
        try:
            res = await self.cc.set_position_mode(hedged=True)
            logging.info(f"hedge mode set {res}")
        except Exception as e:
            logging.error(f"error setting hedge mode: {e}")
        try:
            res = await self.cc.set_leverage(int(self.leverage), symbol=self.symbol)
            logging.info(f"leverage set {res}")
        except Exception as e:
            logging.error(f"error setting leverage: {e}")
