# T-Invest Ubuntu Collector v1.0

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

## Ограничения

- Реальная авторизация на вашем счёте ещё не проверена. Юнит-тесты используют синтетические данные.
- Котировки облигаций — проценты от остаточного номинала без НКД. Учитывайте валюту, амортизацию, комиссии и дату расчётов.
- История операций ограничена 100 страницами за один запуск, при неполноте будет предупреждение.
- Инструмент не подключает ВТБ, его отчёты анализируются отдельно.
- Если возникают ошибки TLS, не отключайте проверку сертификатов.
- Для сбора данных необходим доступ Ubuntu к `invest-public-api.tbank.ru`.

Официальная документация: https://developer.tbank.ru/invest/intro/intro
