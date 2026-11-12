# T-Invest Ubuntu Collector v1.3.1

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

## Модельный агент вместо механической стратегии (v1.3)

**Цель:** отдельный экземпляр модели OpenAI получает реальные рыночные данные из T-Invest Sandbox каждые пять минут и выбирает, какую виртуальную акцию купить, продать или не трогать, сколько лотов и почему. Система не ограничивается алгоритмами EMA/RSI: это признаки во входном контексте для рассуждения модели. Решение по каждой проверке новое; последние решения сохраняются локально.

**Важно:** ChatGPT в текущем диалоге не работает непрерывно на Ubuntu. Для самостоятельных решений требуется отдельная подписка/оплата [OpenAI API](https://platform.openai.com/api-keys) и отдельный ключ. Использование API **не входит автоматически** в подписку ChatGPT. ИИ-решения выполняются новой моделью через API, а не текущим сеансом чата. По умолчанию \`gpt-6.1-sol\`. Не ожидайте гарантированной прибыли; результат песочницы плохо переносится на реальный рынок.

### Установка v1.3

~~~bash
cd ~
wget -O tinvest_ubuntu_collector_v1.3.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/download/v1.3/tinvest_ubuntu_collector.zip
unzip -o tinvest_ubuntu_collector_v1.3.zip
cd tinvest_ubuntu_collector
bash install_ai_agent.sh
~~~

Установщик сначала **отключает прежний алгоритм** \`tinvest-sandbox-autotrader.timer\` и устанавливает модельного агента, но НЕ включает его, пока нет локального API-ключа OpenAI. Это предотвращает одновременную торговлю двумя ботами.

Создайте секретный API-ключ через https://platform.openai.com/api-keys, подключите отдельно биллинг API и введите ключ **только в Ubuntu**:

~~~bash
python3 ~/tinvest-collector/ai_sandbox_agent.py setup
systemctl --user enable --now tinvest-sandbox-ai.timer
sudo loginctl enable-linger "$(whoami)"
~~~

Проверка:

~~~bash
systemctl --user list-timers 'tinvest-sandbox-*'
journalctl --user -u tinvest-sandbox-ai.service -n 70 --no-pager
python3 ~/tinvest-collector/ai_sandbox_agent.py status
~~~

**Сразу остановить:**

~~~bash
systemctl --user disable --now tinvest-sandbox-ai.timer
~~~

Условия защиты: допускаются только виртуальные поручения \`SandboxService/PostSandboxOrder\`; модель не получает инструмента исполнения напрямую. Только разрешённые рублёвые акции TQBR. После каждой рекомендации бот проверяет валюту, лоты, баланс, суммарный риск, открытые поручения и лимиты. Никаких коротких продаж, маржинальной торговли или реальных счетов. При сбое получения данных или ответа модели **не торгуем**. Не более восьми виртуальных заявок и 110 обращений к модели в торговый день. Перед размещением происходит повторная проверка баланса. Денежный резерв по умолчанию от 850 ₽, лимит на новую позицию 42% и на общую экспозицию 82%. Модель оценивает доступные символы из списка \`watchlist\`.

Модель имеет доступ к измеренным свечам и портфелю, но **новостная лента пока не интегрирована**: нельзя полагаться, что она знает последние корпоративные новости. Операционная эффективность не проверялась на реальном брокерском счёте и не гарантируется. Пока не подтверждены правильные ответы API и виртуальные поручения на Ubuntu — это экспериментальная версия.

Файлы \`~/.config/tinvest-readonly/openai-api-key\`, \`sandbox-token\`, \`ai-agent.json\` и отчёты портфеля никогда не публикуйте в GitHub. Модель получает только обезличенные финансовые показатели и информацию о биржевых инструментах — без токена, номеров счетов и истории платежных реквизитов. Журнал решений хранится локально в \`~/.local/state/tinvest-sandbox-trader/ai-decisions.jsonl\`.

### Почему модель, а не механические правила

Программа снабжает модель выборкой свечей, EMA, RSI, динамикой двух часов, текущим портфелем и предыдущими рекомендациями; модель сама формирует конкретную торговую гипотезу и размер заявки. Технические индикаторы — исходные данные, но не условия автоматического входа. Независимый модуль валидации предотвращает операции с неразрешёнными инструментами, недоступными лотами и превышением лимитов.

## Исправление ошибочного запроса GetCandles (v1.3.1)

Если агент показывает `HTTP 400 для trader_candles`, обновите сборщик до v1.3.1. В запросе пятиминутных свечей теперь используется консервативное окно 36 часов вместо семи суток, убран необязательный параметр `candleSourceType`, добавлена безопасная диагностика текста ошибки без токена.

**После установки автоматический таймер НЕ запускается**, пока не выполнен успешный однократный dry-run. Остановите его до обновления:

```bash
systemctl --user disable --now tinvest-sandbox-ai.timer
cd ~
wget -O tinvest_ubuntu_collector_v1.3.1.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/download/v1.3.1/tinvest_ubuntu_collector.zip
unzip -o tinvest_ubuntu_collector_v1.3.1.zip
cd tinvest_ubuntu_collector
bash install_ai_agent.sh
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" python3 ~/tinvest-collector/ai_sandbox_agent.py dry-run
```

Если в журнале появилась запись `ai_decision`, включите периодический запуск:

```bash
systemctl --user enable --now tinvest-sandbox-ai.timer
```

Если снова возникает HTTP 400, сохраните именно сообщение об ошибке после обновления: новый сборщик добавляет диагностическую информацию. Это ещё не подтверждённая реальным Sandbox проверка, потому что токен хранится исключительно на вашем Ubuntu.

## Ограничения

- Реальная авторизация на вашем счёте ещё не проверена. Юнит-тесты используют синтетические данные.
- Котировки облигаций — проценты от остаточного номинала без НКД. Учитывайте валюту, амортизацию, комиссии и дату расчётов.
- История операций ограничена 100 страницами за один запуск, при неполноте будет предупреждение.
- Инструмент не подключает ВТБ, его отчёты анализируются отдельно.
- Если возникают ошибки TLS, не отключайте проверку сертификатов.
- Для Production требуется доступ Ubuntu к `invest-public-api.tbank.ru`, для Sandbox — к `sandbox-invest-public-api.tbank.ru`.

Официальная документация: https://developer.tbank.ru/invest/intro/intro
