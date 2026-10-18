# PrintBot — печать из Telegram

Кто-то отправляет боту файл, сервер на Ubuntu сразу печатает его на HP LaserJet по USB.

- Форматы: PDF, Word (.doc, .docx, .odt, .rtf), JPG, PNG и обычные фото.
- Копии: число в подписи к файлу (`2`, `x2`, `2 шт`, `2 копии`). «Накладная №5» копиями не считается.
- Доступ: новый человек пишет боту, тебе приходит сообщение с кнопками «Разрешить» и «Отклонить».

## Установка

### 1. Создай бота
В Telegram открой **@BotFather** → `/newbot` → придумай имя → скопируй **токен**.

### 2. Скачай проект на сервер
```
git clone https://github.com/Nurlan-byte/telegram-printbot.git ~/telegram-bot
```

### 3. Установи
На сервере:
```
cd ~/telegram-bot
sudo bash install.sh
```
Скрипт поставит CUPS, драйверы HP, LibreOffice и шрифты, создаст службу `printbot`.

### 4. Подключи принтер
Подключи HP LaserJet по USB и включи его, затем:
```
sudo hp-setup -i          # выбрать USB, свой принтер, драйвер по умолчанию
lpstat -p                 # посмотреть имя принтера
sudo lpadmin -d ИМЯ       # сделать его принтером по умолчанию
lp /usr/share/cups/data/testprint   # пробная страница
```
Если пробная страница вышла, принтер готов.

#### HP LaserJet Professional M1132 MFP
У этой модели две особенности.

**1. Принтер виден как флешка «Smart Install».** `lsusb -v -d 03f0:042a` показывает `Mass Storage` вместо `Printer`, а `lpinfo -v` его не находит. `install.sh` ставит правило udev, которое переключает принтер автоматически. Если он подключён до установки, переключи вручную или переподключи кабель:
```
sudo usb_modeswitch -v 0x03f0 -p 0x042a -c /etc/usb_modeswitch.d/03f0:042a
```

**2. Нужен закрытый плагин HP.** С сайта HP скачивание может не работать (ошибка 403) или `hp-plugin` зависает. Тогда скачай плагин с зеркала OpenPrinting и поставь из файла:
```
mkdir -p /tmp/hpplug && cd /tmp/hpplug
V=$(dpkg-query -W -f='${Version}' hplip | cut -d+ -f1)   # например 3.24.4
for f in hplip-$V-plugin.run hplip-$V-plugin.run.asc; do
  curl -fLO https://www.openprinting.org/download/printdriver/auxfiles/HP/plugins/$f
done
sudo hp-plugin -i -p /tmp/hpplug/
```
Затем добавь принтер (серийный номер возьми из `sudo lpinfo -v`):
```
sudo lpadmin -p HP_M1132 -E -v "hp:/usb/HP_LaserJet_Professional_M1132_MFP?serial=СЕРИЙНЫЙ" \
  -m drv:///hpcups.drv/hp-laserjet_professional_m1132_mfp.ppd -o media=A4 -o printer-error-policy=retry-job
sudo lpadmin -d HP_M1132
```
и впиши `PRINTER_NAME=HP_M1132` в `/opt/printbot/.env`.

#### Сервер — виртуальная машина
Принтер нужно пробросить в VM по USB. В VMware включи для него автоподключение к VM, иначе после переподключения кабеля он достанется хост-системе.

### 5. Впиши токен и себя как администратора
```
sudo nano /opt/printbot/.env      # BOT_TOKEN=токен
sudo systemctl restart printbot
```
Отправь боту `/id`, впиши полученное число в `ADMIN_IDS=` в том же файле и снова:
```
sudo systemctl restart printbot
```

### 6. Раздай ссылку на бота
Люди нажимают «Старт», ты одобряешь кнопкой, дальше они просто присылают файлы.

## Команды бота
| Команда | Кто | Что делает |
|---|---|---|
| `/status` | все | состояние принтера и очередь |
| `/id` | все | показать свой Telegram ID |
| `/users` | админ | кому открыт доступ |
| `/remove <id>` | админ | закрыть доступ |
| `/clear` | админ | отменить все задания в очереди |

## Обслуживание
```
journalctl -u printbot -f          # логи в реальном времени
sudo systemctl restart printbot    # перезапуск
sudo systemctl status printbot     # работает ли
```
Обновление: `cd ~/telegram-bot && git pull && sudo bash install.sh`. Настройки в `.env` и список пользователей сохранятся.

## Если что-то не печатает
- `lpstat -p` пишет «disabled» → `sudo cupsenable ИМЯ`
- задания висят в очереди → проверь бумагу/кабель, `/clear` в боте
- Word печатается с «поехавшей» вёрсткой → в документе редкий шрифт; надёжнее присылать PDF
