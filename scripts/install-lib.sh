# Общее для установщиков (source из build-app.sh и install-worker.sh; ROOT задаёт вызывающий):
# каталог данных, префикс меток launchd и bundle id, подстановка в шаблоны launchd/ и Info.plist.
DATA_DIR="${TRANSCRIBER_DATA:-$HOME/Library/Application Support/Transcriber}"

# Префикс: TRANSCRIBER_LABEL_PREFIX > строка LABEL_PREFIX=… в $DATA_DIR/install.env > local.transcriber.
# Из него: bundle id приложения, метки <prefix>.app / <prefix>.worker, UID агрегатного устройства.
# Сменить префикс на живой установке = заново выдать разрешения macOS (микрофон, запись звука,
# универсальный доступ): они привязаны к bundle id.
_label_prefix_from_file() {
  if [ -f "$DATA_DIR/install.env" ]; then
    sed -n 's/^LABEL_PREFIX=//p' "$DATA_DIR/install.env" | tail -n 1 | tr -d "\"'"
  fi
}
LABEL_PREFIX="${TRANSCRIBER_LABEL_PREFIX:-$(_label_prefix_from_file)}"
LABEL_PREFIX="${LABEL_PREFIX:-local.transcriber}"
if ! [[ "$LABEL_PREFIX" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]]; then
  echo "LABEL_PREFIX «$LABEL_PREFIX»: нужен обратный домен из букв, цифр и дефисов, например local.transcriber" >&2
  return 1 2>/dev/null || exit 1
fi

render_template() {   # render_template <шаблон> <куда>
  sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$ROOT|g" -e "s|__LABEL_PREFIX__|$LABEL_PREFIX|g" "$1" > "$2"
}
