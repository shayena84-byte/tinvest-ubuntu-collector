# T-Invest Ubuntu Collector v1.2

Сборщик **T-Invest API → Ubuntu Server → приватный ZIP-отчёт** на Python 3 без внешних библиотек. Только чтение: позиции, остатки, операции, тикеры/ISIN, последние котировки и стакан по ISIN. **Заявки не выставляет.**

## Установка напрямую на Ubuntu

Используйте архив исходников, автоматически создаваемый GitHub для ветки `main`.

```bash
sudo apt update && sudo apt install -y python3 unzip curl
cd "$HOME"
curl -fL --retry 3 -o tinvest-ubuntu-collector.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/archive/refs/heads/main.zip
unzip -o tinvest-ubuntu-collector.zip
cd tinvest-ubuntu-collector-main
python3 -m unittest discover -s tests -v
bash install.sh
```

После установки создайте **отдельный Read-only токен** в [настройках T-Invest API](https://www.tbank.ru/invest/settings/api/) и выполните:

```bash
python3 ~/tinvest-collector/collector.py setup
python3 ~/tinvest-collector/collector.py accounts
python3 ~/tinvest-collector/collector.py snapshot --days 90
```

Токен вводится интерактивно и **не отображается в терминале**; хранится только на Ubuntu в файле `~/.config/tinvest-readonly/token` с правами доступа 600. **Никогда не отправляйте токен в чат и не публикуйте на GitHub.**

## Другие команды

```bash
python3 ~/tinvest-collector/collector.py snapshot --days 365
python3 ~/tinvest-collector/collector.py snapshot --no-ops
python3 ~/tinvest-collector/collector.py quote RU000A1092T7 --depth 10
```

Команда `accounts` показывает псевдонимы счетов, а не реальные номера. `snapshot` создаёт архив `~/tinvest-reports/tinvest_snapshot_*.zip` с `snapshot.json`, `positions.csv`, `operations.csv` и `README.txt`.

**Важно:** итоговый ZIP содержит конфиденциальные финансовые сведения и **не зашифрован**. Его нельзя публиковать на GitHub. Для анализа загрузите этот ZIP отдельно в приватную беседу ChatGPT; автоматической передачи данных нет.

## Проверка песочницы T-Invest (Sandbox)

**Sandbox полностью отделён от реального счёта.** Используется другой адрес API и отдельный файл токена `~/.config/tinvest-readonly/sandbox-token`. Все действия `open` и `fund` выполняются только с виртуальными счетами.

Если ранее создали токен Sandbox через обычный `collector.py setup`, один раз перенесите его в отдельный файл:

```bash
mv ~/.config/tinvest-readonly/token ~/.config/tinvest-readonly/sandbox-token
chmod 600 ~/.config/tinvest-readonly/sandbox-token
```

Если токен ранее не сохраняли, выполните `python3 ~/tinvest-collector/sandbox.py setup`. **Не переносите Production-токен в sandbox-token.**

Подключение к Sandbox с сохранением проверки TLS и вашего отдельного CA bundle:

```bash
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" python3 ~/tinvest-collector/sandbox.py accounts
```

Если открытых счетов нет, создайте виртуальный и проверьте его:

```bash
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" python3 ~/tinvest-collector/sandbox.py open
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" python3 ~/tinvest-collector/sandbox.py accounts
```

Пополнить выбранный счёт на 100 000 **виртуальных** рублей (замените `tb_...` псевдонимом из `accounts`):

```bash
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" python3 ~/tinvest-collector/sandbox.py fund tb_123456789abc 100000
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" python3 ~/tinvest-collector/sandbox.py snapshot --days 7
```

Архивы Sandbox сохраняются отдельно в `~/tinvest-reports/sandbox/`. Данные песочницы не являются реальными позициями брокерского счёта. Торговых методов даже в Sandbox в сборщике нет: только чтение, создание и виртуальное пополнение счёта.

Официальная документация: https://developer.tbank.ru/invest/intro/developer/sandbox/.

## Автоматическая торговля в Sandbox каждые 5 минут (v1.2)

**Экспериментальная стратегия.** Используется только виртуальный счёт, привязанный к Sandbox-токену; никакого доступа к реальным деньгам или заявкам. Это программный алгоритм EMA(9/21) + RSI(14) + импульс за 30 минут, а не непрерывное управление сделками моделью ChatGPT. Он может не заработать, не торговать при отсутствии сигналов или потерять все виртуальные деньги.

Для установки обновлённого пакета скачайте релиз v1.2 и запустите установщик **от обычного пользователя Ubuntu**, указав обезличенный номер Sandbox-счёта:

~~~bash
cd ~
wget -O tinvest_ubuntu_collector_v1.2.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/download/v1.2/tinvest_ubuntu_collector.zip
unzip -o tinvest_ubuntu_collector_v1.2.zip
cd tinvest_ubuntu_collector
bash install_autotrader.sh tb_3dcf45aba0ff
~~~

Установщик делает offline-тесты, копирует код в \`~/tinvest-collector\`, создаёт приватную конфигурацию \`~/.config/tinvest-readonly/autotrader.json\` и активирует \`systemd --user\` timer с интервалом 5 минут. Он **не переносит** токен в GitHub. Используется существующий файл \`~/.config/tinvest-readonly/sandbox-token\` и индивидуальный TLS bundle.

Чтобы таймер продолжал работать после выхода из SSH:

~~~bash
sudo loginctl enable-linger "$(whoami)"
~~~

Диагностика:

~~~bash
systemctl --user list-timers 'tinvest-sandbox-*'
journalctl --user -u tinvest-sandbox-autotrader.service -n 60 --no-pager
python3 ~/tinvest-collector/sandbox_autotrader.py status
~~~

**Сразу остановить автоматическую торговлю:**

~~~bash
systemctl --user disable --now tinvest-sandbox-autotrader.timer
~~~

Дополнительное выключение — изменить \`"enabled": false\` в файле \`~/.config/tinvest-readonly/autotrader.json\`.

Общая схема ограничений: только ликвидные рублёвые акции \`SBER\`, \`MOEX\`, \`YDEX\`; не более 42% текущего капитала в одной новой позиции, общий лимит 82%, денежный резерв от 850 ₽, не более 8 заявок в день, пауза после 8% дневной просадки, стоп-лосс 2,5%, фиксация прибыли 4,5%, трейлинг-стоп 2,5%, минимальный интервал между заявками 20 минут. Торговля по умолчанию ограничена рабочими днями и временем 10:10–18:35 МСК. Скрипт может совершать 0 сделок за день, если условия не выполнены.

Состояние и журналы хранятся локально в \`~/.local/state/tinvest-sandbox-trader/\`. Скрипт не допускает новых сделок при неизвестном результате предыдущей заявки. Песочница исполняет рыночные заявки по последней биржевой сделке, моделирует комиссию 0,05% и **не** начисляет купоны/дивиденды. Результаты в Sandbox не гарантируют доходности на реальном рынке.

## Ограничения

- Реальная авторизация на вашем счёте ещё не проверена. Юнит-тесты используют синтетические данные.
- Котировки облигаций — проценты от остаточного номинала без НКД. Учитывайте валюту, амортизацию, комиссии и дату расчётов.
- История операций ограничена 100 страницами за один запуск, при неполноте будет предупреждение.
- Инструмент не подключает ВТБ, его отчёты анализируются отдельно.
- Если возникают ошибки TLS, не отключайте проверку сертификатов.
- Для Production требуется доступ Ubuntu к `invest-public-api.tbank.ru`, для Sandbox — к `sandbox-invest-public-api.tbank.ru`.

Официальная документация: https://developer.tbank.ru/invest/intro/intro
