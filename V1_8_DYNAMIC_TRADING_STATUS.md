# T-Invest Collector v1.8.0 — динамический торговый статус вместо часов 10:10–18:35

## Что изменено

В OpenAI Trader v1.7.0 вход в рыночный анализ блокировался вызовом market_hours(): только будние дни с 10:10 до 18:35 МСК. Даже если SBER/MOEX или другая бумага торговалась в вечерней сессии, агент отвечал openai_market_closed до обращения к T-Invest.

В v1.8.0 **нет временного ограничения на основе дня недели или часов**. Для каждого разрешённого инструмента по его точному UID и доске TQBR выполняется MarketDataService/GetTradingStatus в T-Invest Sandbox.

Для рыночной заявки **обязательны все три условия**:
- tradingStatus = SECURITY_TRADING_STATUS_NORMAL_TRADING;
- marketOrderAvailableFlag = true;
- apiTradeAvailableFlag = true.

Дополнительно проверяется UID ответа. Любой пропуск поля, ошибка API, открытие/закрытие аукциона, приостановка торгов, несоответствие UID или запрет рыночных заявок — **запрет торговли** (fail-closed).

Это учитывает фактическую вечернюю сессию, праздники, возможные специальные торги и разные режимы отдельных бумаг, а не предполагаемый календарь. Статус проверяется перед сбором рыночного контекста и повторно дважды после решения модели, перед самой отправкой виртуального ордера.

Не меняются:
- реальный брокерский счёт: недоступен, используем только Sandbox;
- OpenAI Responses API и уже установленный API-ключ;
- ProxyChains4 / SOCKS5 192.168.202.50:1080;
- проверка актуальности стакана, спреда, объёмов;
- финансовые лимиты на виртуальные сделки и защита от дублей;
- SQLite, десять уровней стакана, дашборд и пятиминутный таймер.

## Установить поверх v1.7.0

~~~bash
cd ~
wget -O tinvest_ubuntu_collector_v1.8.0.zip https://github.com/shayena84-byte/tinvest-ubuntu-collector/releases/download/v1.8.0/tinvest_ubuntu_collector.zip
unzip -o tinvest_ubuntu_collector_v1.8.0.zip
cd tinvest_ubuntu_collector
bash upgrade_dynamic_session.sh
~~~

Установщик выключает таймер OpenAI, чтобы не допустить фонового запуска во время обновления. Если текущий сервис всё ещё активен, обновление прерывается во избежание замены кода в процессе. Далее проводятся офлайн-тесты, создаётся резервная копия старого торгового Python-файла и обновляются только trading_session.py и openai_quality_trader.py.

**Обновление не включает таймер автоматически.** Действующий systemd override proxy.conf, содержащий команду ProxyChains, остаётся неизменным. Установщик не меняет порт, прокси, TLS bundle, ключи, счёт, конфигурацию торгов или базу SQLite.

## Проверить статус торгов без OpenAI и без сделок

~~~bash
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" \
proxychains4 -f "$HOME/.config/tinvest-readonly/proxychains.conf" \
python3 ~/tinvest-collector/openai_quality_trader.py session
~~~

Вы увидите строку для каждой акции:

- SBER OPEN - normal_trading_verified — подтверждён нормальный режим и разрешены заявки через API;
- MOEX CLOSED - trading_status_session_close — сессия закрыта;
- YDEX CLOSED - market_orders_not_available — рыночные заявки запрещены;
- LKOH CLOSED - status_probe_error — статус не удалось проверить, действуем безопасно.

Ни одна из строк не означает, что сделка обязательно исполнится. Цена и реальная ликвидность проверяются отдельно.

## Тест решения ИИ без торговых поручений

~~~bash
SSL_CERT_FILE="$HOME/.config/tinvest-readonly/ca-bundle.pem" \
proxychains4 -f "$HOME/.config/tinvest-readonly/proxychains.conf" \
python3 ~/tinvest-collector/openai_quality_trader.py dry-run
~~~

Если хотя бы одна бумага открыта и прошла проверку свежести котировок, система формирует торговый контекст и запрашивает решение OpenAI. Но **никаких заявок в dry-run не подаётся**. Если все бумаги закрыты, результат будет openai_market_closed с причиной no_verified_open_instruments. Это нормальное безопасное поведение.

## Включить торговлю в виртуальной песочнице

Только после успешной проверки статусов и dry-run:

~~~bash
systemctl --user enable --now tinvest-sandbox-openai.timer
systemctl --user status tinvest-sandbox-openai.timer --no-pager
journalctl --user -u tinvest-sandbox-openai.service -n 100 --no-pager
~~~

Агент проверяет статус при каждом запуске, в том числе вечером. Таймер продолжает работать каждые 5 минут даже при закрытом рынке, но при отсутствии открытых акций не вызывает OpenAI и не подаёт заявку. Плата за API возникает лишь при фактическом запросе модели.

Для остановки:

~~~bash
systemctl --user disable --now tinvest-sandbox-openai.timer
~~~

## Если рынки не открываются

1. Убедитесь, что API T-Invest возвращает статус для точного UID TQBR.
2. Проверьте session, не отключая проверку apiTradeAvailableFlag, marketOrderAvailableFlag или состояния торгов.
3. Проверьте журнал market_skipped и качество стаканов в локальном дашборде. Свежий статус NORMAL сам по себе не делает рыночные данные достоверными.
4. Ошибки T-Invest при GetTradingStatus блокируют торговлю. Временные окна не используются как подмена неуспешного ответа.

**Важно:** моделирование в Sandbox не гарантирует прибыль. Наблюдение фактически открытой вечерней сессии не означает, что в ней есть достаточная ликвидность или безопасная цена исполнения.

Официальная спецификация: https://developer.tbank.ru/invest/services/quotes/marketdata/
