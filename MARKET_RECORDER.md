# Рыночный архив SQLite — T-Invest Sandbox Market Recorder v1.4.0

Автоматический, **только читающий** сборщик рыночных данных из T-Invest Sandbox. Не обращается к OpenAI, не выставляет торговые заявки, не изменяет реальный или виртуальный брокерский счёт. Работает на Ubuntu 22.04/24.04 через уже настроенный proxychains4 с SOCKS5 **192.168.202.50:1080**.

## Что собирает

По умолчанию:
- **SBER**, торговая доска **TQBR**;
- **Омега БО-01**, ISIN **RU000A1092T7**, доска **TQCB**.

Раз в пять минут записываются:
- полученная цена последней сделки и время последней сделки биржи;
- цена лучшей заявки покупателя и продавца, размер спреда в процентах от средней цены;
- до **10 уровней** bid и ask с размерами в лотах;
- суммарные объёмы первых десяти уровней и объём лучших заявок;
- момент фактического получения, 5-минутная корзина UTC, возраст последней сделки и стакана;
- диагностические записи ошибок отдельно от успешных снимков.

**Различайте время сбора и время биржевых данных**: вне торгов стакан может оставаться устаревшим. Замеры сохраняются с отдельными полями book_stale_seconds и last_stale_seconds. Это не поток отдельных сделок с точностью до секунды.

## Однократная установка

~~~bash
cd ~
wget -O tinvest_ubuntu_collector_v1.4.0.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/download/v1.4.0/tinvest_ubuntu_collector.zip
unzip -o tinvest_ubuntu_collector_v1.4.0.zip
cd tinvest_ubuntu_collector
bash install_market_recorder.sh
sudo loginctl enable-linger "$(whoami)"
~~~

Установщик:
1. Проверяет наличие Sandbox-токена, сертификатов и конфигурации SOCKS5;
2. Выполняет синтаксические проверки и все unit-тесты без сети;
3. Создаёт **отдельные** user service и timer (tinvest-sandbox-market) и конфигурацию market-recorder.json;
4. Выключает ранее созданные AI- и EMA-таймеры (не затрагивает API-ключи или отчёты);
5. **Сначала пробует собрать данные через Sandbox**; активирует таймер только если оба инструмента успешно сохранены.

Используется существующий Sandbox-токен в приватном файле ~/.config/tinvest-readonly/sandbox-token. Никаких паролей/токенов в SQLite не записывается. Весь сетевой обмен именно этого сервиса направляется через SOCKS5, общая маршрутизация Ubuntu не меняется.

## Управление и анализ

~~~bash
systemctl --user list-timers 'tinvest-sandbox-market*'
systemctl --user status tinvest-sandbox-market.timer --no-pager
journalctl --user -u tinvest-sandbox-market.service -n 60 --no-pager
python3 ~/tinvest-collector/market_recorder.py report --hours 24
python3 ~/tinvest-collector/market_recorder.py report --hours 168
python3 ~/tinvest-collector/market_recorder.py export --hours 168
~~~

CSV создаётся в ~/tinvest-reports/market/market_YYYYMMDD_HHMMSS.csv с правами 600. Для просмотра SQL:

~~~bash
sudo apt install -y sqlite3
sqlite3 ~/.local/share/tinvest-market/market.sqlite3 \
  'SELECT i.ticker,s.collected_utc,s.last_price,s.best_bid,s.best_ask,s.spread_pct,s.bids_total,s.asks_total FROM snapshots s JOIN instruments i ON i.uid=s.uid ORDER BY s.collected_utc DESC LIMIT 12;'
~~~

Сама программа использует встроенный Python SQLite и сторонних библиотек не требует.

**Остановить сбор:**

~~~bash
systemctl --user disable --now tinvest-sandbox-market.timer
~~~

База хранится в ~/.local/share/tinvest-market/market.sqlite3 и **не удаляется** при остановке. Для безопасного резервного копирования при активном сборе используйте sqlite3 .backup, а не простое копирование WAL-базы.

## Таблицы базы данных

- instruments — UID, тикер, ISIN, класс торгов, тип инструмента;
- snapshots — один снимок на UID и 5-минутный интервал, bid/ask, спред, время и возраст;
- book_levels — конкретные уровни с ценами и количеством лотов;
- fetch_errors — диагностические ошибки сбора.

Уникальность пары (uid, slot_utc) исключает дублирование при повторном запуске. Обновление снимка и его уровней происходит **в одной транзакции**. Время сохранено в UTC, без проблем перехода летнего/зимнего времени.

## Изменение списка бумаг

Измените ~/.config/tinvest-readonly/market-recorder.json, добавив инструменты с **точным кодом торговой доски**. Максимум 10 инструментов; глубина стакана от 1 до 20.

~~~json
{
  "depth": 10,
  "instruments": [
    {"symbol":"SBER","board":"TQBR","kind":"share"},
    {"symbol":"RU000A1092T7","board":"TQCB","kind":"bond"}
  ]
}
~~~

Не меняйте kind и board случайно: правильная бумага проверяется по UID, ISIN и торговой доске.

## Границы точности

- Методам GetLastPrices и GetOrderBook требуются API-токен и интернет-доступ; рыночные данные могут иметь задержку.
- Вне торгов и при низкой ликвидности книга может быть пустой или не обновляться — такие данные сохраняются с указанным возрастом.
- Объёмы записаны в **лотах**, а цены облигаций — в **процентах от остаточного номинала без НКД**; для рублёвых сумм нужен отдельный расчёт.
- В Sandbox используется реальная рыночная информация, но торговое исполнение отличается от реального.
- База накапливается; организуйте резервное копирование и контролируйте свободное место.
- Автоматические тесты GitHub проверяют алгоритм и схему БД на искусственных ответах. Успешный запуск на вашем Ubuntu нужно подтвердить журналом.

Официальная документация:
- https://developer.tbank.ru/invest/api/market-data-service-get-last-prices
- https://developer.tbank.ru/invest/api/market-data-service-get-order-book
- https://developer.tbank.ru/invest/intro/developer/sandbox
