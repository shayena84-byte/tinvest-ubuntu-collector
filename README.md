# T-Invest Ubuntu Collector v1.11.0

Инвестиционная система на Ubuntu: сбор рыночных данных, история стаканов в SQLite,
локальная панель, модельные решения и виртуальная торговля **только T-Invest Sandbox**.
Python 3.10+ и стандартная библиотека; внешние Python-пакеты не требуются.

Последний выпуск: [v1.11.0](https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/tag/v1.11.0).
Полная инструкция обновления, настройки и ограничений — [V1_11_OPERATIONS.md](V1_11_OPERATIONS.md).

## Что входит

- Котировки и 10 уровней стакана, проверка качества и актуальности биржевого времени.
- Локальная панель: цены, глубина, качество, службы, бюджет API, решения и исполнения.
- Общие риск-лимиты и проверяющий модуль для OpenAI, Ollama и исторического агента.
- Отдельный наблюдатель за портфелем/заявками без зависимости от модели и её биллинга.
- SQLite-журнал данных, решений, причин отказа, заявок, частичных исполнений и комиссий.
- Автономный аудит и причинная симуляция по последующим историческим стаканам.
- Атомарный бюджет всех генераций, включая `dry-run`; неизменяемые релизные архивы.

## Установка и обновление

```bash
cd ~
curl -fL --retry 3 -o tinvest_v1.11.0.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/download/v1.11.0/tinvest_ubuntu_collector.zip
mkdir -p tinvest-v1.11.0-source
unzip -o tinvest_v1.11.0.zip -d tinvest-v1.11.0-source
cd tinvest-v1.11.0-source/tinvest_ubuntu_collector
bash install_v1_11.sh
python3 ~/tinvest-collector/system_status.py
```

Запускать от обычного пользователя Ubuntu. Установщик проверяет комплектность,
тесты и импорт установленного кода, делает резервные копии, сохраняет ключи,
SOCKS5, TLS bundle, настройки и историю. Торговые таймеры остаются выключенными.
SHA-256 архива и проверка контрольной суммы описаны в полной инструкции.
Для новой установки без ранее настроенного счёта использовать существующие
команды `sandbox.py setup`, `accounts` и `open`; реальный токен не переносить в Sandbox.
Псевдоним виртуального счёта передать как аргумент `bash install_v1_11.sh tb_...`.
Финансирование виртуального счёта выполняется отдельно командой `sandbox.py fund`.

## Работа без OpenAI

```bash
python3 ~/tinvest-collector/v19_health.py
python3 ~/tinvest-collector/offline_market_audit.py --hours 168
python3 ~/tinvest-collector/offline_replay.py --hours 168 --strategy compare
python3 ~/tinvest-collector/system_status.py
```

Только наблюдателю нужны сеть и Sandbox-токен. Его режим по умолчанию `observe`
не отправляет и не отменяет брокерские заявки:

```bash
systemctl --user enable --now tinvest-sandbox-supervisor.timer
```

При новой установке сбор котировок и панель включаются отдельно:

```bash
systemctl --user enable --now tinvest-sandbox-market.timer tinvest-sandbox-dashboard.service
sudo loginctl enable-linger "$(whoami)"
```

Они используют существующую настройку SOCKS5 и TLS. Панель доступна только на
`http://127.0.0.1:8787`; для Windows — SSH-туннель:

```bash
ssh -L 8787:127.0.0.1:8787 user@IP_UBUNTU
```

## Модельные решения

Отдельный ключ OpenAI API хранится только в `~/.config/tinvest-readonly/openai-api-key`;
Sandbox-токен — отдельно в `sandbox-token`. Подписка ChatGPT и оплата API независимы.
Модель получает обезличенные показатели и возвращает JSON; брокерские инструменты
ей не предоставляются. Ключ, существующая выбранная модель и настройки сохраняются.
Ollama — опциональный провайдер; установка её не требует и не загружает моделей.

Общие потолки: покупка до 10% капитала, экспозиция до 50%, дневной порог потерь 3%,
до 3 виртуальных заявок и 24 генераций за день МСК. Между генерациями по умолчанию
30 минут; ошибки и `dry-run` тоже учитываются. Уверенность модели не является
измеренной вероятностью прибыли. Проверяющий модуль независимо проверяет лоты,
баланс, свежий стакан, статус торгов и лимиты перед отправкой лимитной Sandbox-заявки.

После проверки доступа, биллинга, данных и одного `dry-run` оператор может включить:

```bash
systemctl --user enable --now tinvest-sandbox-openai.timer
```

Остановка:

```bash
systemctl --user disable --now tinvest-sandbox-openai.timer tinvest-sandbox-ai.timer tinvest-sandbox-local-ai.timer tinvest-sandbox-autotrader.timer tinvest-sandbox-supervisor.timer
```

## Данные и ограничения

Ключи, настройки, рабочие базы, журналы и финансовые ZIP-отчёты являются частными;
не публиковать их на GitHub. Панель только читает локальные данные и не управляет
торговлей. Облигационные котировки выражены в процентах номинала без НКД;
симулятор исключает бумаги без подтверждённой лотности и единиц RUB/акция.

Пятиминутные снимки дают приближённые сценарии исполнения, а не точный биржевой
бэктест. Sandbox и симуляции не подтверждают доходность на реальном рынке.
`collector.py` сохраняет отдельный режим чтения реального счёта и приватного
экспорта отчётов; торговых production endpoint в движках нет.

## Разработка и выпуск

```bash
python3 -m unittest discover -s tests -q
node --check dashboard.js
python3 build_release.py --output .build/tinvest_ubuntu_collector.zip
```

Единственный источник номера версии — `version.py`; набор устанавливаемых файлов —
`package_manifest.json`. CI проверяет исходники, упаковку и установку. Релиз собирается
из совпадающего тега `vX.Y.Z`, содержит `BUILD.json` и отдельную SHA-256; существующие
архивы не перезаписываются. Исторические инструкции предыдущих версий сохранены
в отдельных файлах и не являются текущим способом установки.

Официальная документация:
[T-Invest API](https://developer.tbank.ru/invest/intro/intro),
[Sandbox](https://developer.tbank.ru/invest/intro/developer/sandbox),
[тип идентификатора заявки](https://developer.tbank.ru/invest/api/sandbox-service-get-sandbox-order-state).
