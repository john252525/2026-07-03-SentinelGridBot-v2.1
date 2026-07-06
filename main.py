import ccxt
import time
import sqlite3
import json
from datetime import datetime, timedelta

class SentinelGridBot:
    """
    Броневик v2.1: Закрыты уязвимости технадзора.
    - Валидация маржи перед размещением сетки.
    - Аварийный Hard Stop после 4-го колена.
    - EMA вместо SMA для выхода из Караула.
    """

    def __init__(self, symbol, api_key, api_secret, testnet=True):
        self.exchange = ccxt.bybit({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
        })
        if testnet:
            self.exchange.set_sandbox_mode(True)
        
        self.symbol = symbol
        self.base_price = None
        self.mode = "ACTIVE"
        self.grid_levels = 8
        self.step_percent = 0.005
        self.base_order_usdt = 5.0  # Базовый размер в USDT (будет скорректирован)
        self.profit_target = 200.0
        self.stop_loss = -250.0
        self.leverage = 3
        
        self.orders = {}
        self.hard_stop_id = None  # ID стоп-маркет ордера
        
        self.conn = sqlite3.connect('sentinel_bot.db')
        self._init_db()
        self._load_state()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

    def _save_state(self):
        state = {
            'mode': self.mode,
            'base_price': self.base_price,
            'orders': json.dumps(self.orders)
        }
        with self.conn:
            for key, value in state.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                    (key, str(value))
                )

    def _load_state(self):
        cursor = self.conn.execute("SELECT key, value FROM bot_state")
        rows = {row[0]: row[1] for row in cursor.fetchall()}
        if 'mode' in rows:
            self.mode = rows['mode']
            self.base_price = float(rows['base_price']) if rows['base_price'] != 'None' else None
            self.orders = json.loads(rows['orders']) if rows['orders'] else {}

    # ==================== ОСНОВНОЙ ЦИКЛ ====================
    def run(self, interval_seconds=30):
        print(f"Бот v2.1 запущен. Режим: {self.mode}")
        while True:
            try:
                self._update()
            except Exception as e:
                print(f"Ошибка в цикле: {e}. Пауза 60 сек.")
            time.sleep(interval_seconds)

    def _update(self):
        if self._check_kill_switch():
            self._emergency_close()
            return

        if self.mode == "ACTIVE":
            self._update_active()
        elif self.mode == "SENTINEL":
            self._update_sentinel()

        self._save_state()

    # ==================== ПАТЧ 1: ВАЛИДАЦИЯ МАРЖИ ====================
    def _get_available_margin(self):
        """Возвращает свободную маржу на изолированном счете символа."""
        try:
            balance = self.exchange.fetch_balance()
            # Bybit: изолированная маржа лежит в 'info' -> 'coins'
            # Упрощенный вариант: используем общий свободный баланс USDT
            return float(balance['USDT']['free'])
        except Exception as e:
            print(f"Ошибка получения маржи: {e}")
            return 0.0

    def _calculate_required_margin(self, order_size_usdt):
        """Считает, сколько маржи нужно для 8 ордеров с плечом x3."""
        total_exposure = order_size_usdt * self.grid_levels
        return total_exposure / self.leverage

    def _adjust_order_size(self):
        """Уменьшает размер ордера, если маржи не хватает."""
        available = self._get_available_margin()
        required = self._calculate_required_margin(self.base_order_usdt)
        
        if available >= required:
            return self.base_order_usdt
        
        # Считаем, какой максимальный размер ордера мы можем себе позволить
        max_total_exposure = available * self.leverage
        adjusted_size = max_total_exposure / self.grid_levels
        
        # Минимальный порог: не торгуем, если размер меньше $2
        if adjusted_size < 2.0:
            print("КРИТИЧЕСКИ: Недостаточно маржи даже для минимального ордера.")
            return 0.0
        
        print(f"ВНИМАНИЕ: Размер ордера уменьшен с ${self.base_order_usdt} до ${adjusted_size:.2f} из-за нехватки маржи.")
        return adjusted_size

    # ==================== РЕЖИМ "АКТИВ" ====================
    def _update_active(self):
        if not self.orders:
            self._place_grid_orders()
            return

        self._sync_orders()

        # ПАТЧ 2: Проверка необходимости Hard Stop
        self._manage_hard_stop()

        pnl = self._calculate_pnl()

        if pnl <= self.stop_loss:
            self._close_all("STOP_LOSS")
            return

        if pnl >= self.profit_target:
            self._close_all("PROFIT_GOAL")
            return

        if self._is_deep_drawdown() and pnl >= 5.0:
            self._close_all("BREAKEVEN")
            return

    def _place_grid_orders(self):
        # Сначала корректируем размер ордера под доступную маржу
        order_size_usdt = self._adjust_order_size()
        if order_size_usdt == 0.0:
            print("Размещение сетки отменено: нет маржи.")
            self.mode = "SENTINEL"
            return

        ticker = self.exchange.fetch_ticker(self.symbol)
        self.base_price = ticker['last']
        print(f"Размещаем сетку. База: {self.base_price}, Ордер: ${order_size_usdt:.2f}")

        for i in range(self.grid_levels):
            price = self.base_price * (1 - (i * self.step_percent))
            try:
                order = self.exchange.create_limit_buy_order(
                    self.symbol,
                    order_size_usdt / price,
                    price,
                    {'position_idx': 0}
                )
                self.orders[i] = {'id': order['id'], 'price': price, 'filled': False}
                print(f"Ордер {i}: {price} USD")
            except Exception as e:
                print(f"Ошибка размещения ордера {i}: {e}")

    def _sync_orders(self):
        for level, order_info in self.orders.items():
            if order_info['filled']:
                continue
            try:
                order = self.exchange.fetch_order(order_info['id'], self.symbol)
                if order['status'] == 'closed':
                    order_info['filled'] = True
                    print(f"Колено {level} исполнено по {order_info['price']}")
            except Exception as e:
                print(f"Ошибка синхронизации ордера {level}: {e}")

    # ==================== ПАТЧ 2: АВАРИЙНЫЙ HARD STOP ====================
    def _manage_hard_stop(self):
        """Выставляет стоп-маркет ордер после исполнения 4-го колена."""
        filled_levels = [lvl for lvl, info in self.orders.items() if info['filled']]
        
        # Условие активации: исполнено 4 и более колен, а стоп еще не выставлен
        if len(filled_levels) >= 4 and self.hard_stop_id is None:
            # Уровень Hard Stop: чуть ниже последнего (8-го) уровня сетки
            last_grid_price = self.base_price * (1 - (self.grid_levels - 1) * self.step_percent)
            stop_price = last_grid_price * 0.998  # 0.2% ниже сетки, защита от гэпа
            
            try:
                # Получаем текущий размер позиции
                positions = self.exchange.fetch_positions([self.symbol])
                total_contracts = sum(p['contracts'] for p in positions if p['side'] == 'long')
                
                if total_contracts > 0:
                    stop_order = self.exchange.create_order(
                        self.symbol,
                        'stop_market',
                        'sell',
                        total_contracts,
                        None,
                        {'stopPx': stop_price, 'position_idx': 0}
                    )
                    self.hard_stop_id = stop_order['id']
                    print(f"АВАРИЙНЫЙ СТОП ВЫСТАВЛЕН на {stop_price} USD. ID: {self.hard_stop_id}")
            except Exception as e:
                print(f"Ошибка выставления Hard Stop: {e}")

    def _cancel_hard_stop(self):
        """Отменяет стоп-маркет ордер, если он больше не нужен."""
        if self.hard_stop_id:
            try:
                self.exchange.cancel_order(self.hard_stop_id, self.symbol)
                print(f"Hard Stop {self.hard_stop_id} отменен.")
            except:
                pass
            self.hard_stop_id = None

    def _calculate_pnl(self):
        total_pnl = 0.0
        ticker = self.exchange.fetch_ticker(self.symbol)
        current_price = ticker['last']
        for order_info in self.orders.values():
            if order_info['filled']:
                pnl_per_unit = current_price - order_info['price']
                total_pnl += pnl_per_unit * (self.base_order_usdt / order_info['price'])
        return total_pnl

    def _is_deep_drawdown(self):
        return any(
            order_info['filled']
            for level, order_info in self.orders.items()
            if level >= 5
        )

    # ==================== ПАТЧ 3: EMA ВМЕСТО SMA ====================
    def _calculate_ema(self, closes, length=50):
        """Вычисляет EMA вручную по списку цен закрытия."""
        if len(closes) < length:
            return None
        
        # Начальное значение — SMA за первый период
        ema = sum(closes[:length]) / length
        k = 2 / (length + 1)
        
        for close in closes[length:]:
            ema = (close * k) + (ema * (1 - k))
        
        return ema

    def _update_sentinel(self):
        ohlcv = self.exchange.fetch_ohlcv(self.symbol, '15m', limit=60)
        if len(ohlcv) < 55:
            return

        closes = [candle[4] for candle in ohlcv]
        
        # Проверяем, что последняя свеча закрыта (её timestamp не в текущем 15-мин окне)
        last_candle_time = datetime.fromtimestamp(ohlcv[-1][0] / 1000)
        if datetime.now() - last_candle_time < timedelta(minutes=14):
            return

        # Используем честную EMA
        ema_50 = self._calculate_ema(closes, 50)
        if ema_50 is None:
            return

        close_15m = closes[-1]
        if close_15m > ema_50:
            self.mode = "ACTIVE"
            self.orders = {}
            self.base_price = None
            self.hard_stop_id = None
            print("Выход из Караула (EMA пробита). Перезапуск сетки.")

    # ==================== ЗАКРЫТИЕ ПОЗИЦИЙ ====================
    def _close_all(self, reason):
        print(f"Закрытие всех позиций. Причина: {reason}")
        
        # Отменяем Hard Stop
        self._cancel_hard_stop()
        
        # Отменяем неисполненные лимитные ордера
        for order_info in self.orders.values():
            if not order_info['filled']:
                try:
                    self.exchange.cancel_order(order_info['id'], self.symbol)
                except:
                    pass
        
        # Закрываем рыночную позицию
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            for pos in positions:
                if pos['contracts'] > 0:
                    self.exchange.create_market_sell_order(
                        self.symbol,
                        pos['contracts'],
                        {'position_idx': 0}
                    )
        except Exception as e:
            print(f"Ошибка при закрытии позиции: {e}")

        self.orders = {}
        self.hard_stop_id = None

        if reason == "PROFIT_GOAL":
            self.mode = "ACTIVE"
            self.base_price = None
        else:
            self.mode = "SENTINEL"
            print("Бот уходит в Караул.")

    def _emergency_close(self):
        self._close_all("KILL_SWITCH")
        print("Kill Switch активирован. Сон на 1 час.")
        time.sleep(3600)

    def _check_kill_switch(self):
        return False

# ==================== ТОЧКА ВХОДА ====================
if __name__ == "__main__":
    API_KEY = "your_testnet_api_key"
    API_SECRET = "your_testnet_api_secret"
    
    bot = SentinelGridBot(
        symbol="XAUUSDT",
        api_key=API_KEY,
        api_secret=API_SECRET,
        testnet=True
    )
    
    bot.run()
