# Общее для установщиков (source из build-app.sh и install-worker.sh; ROOT задаёт вызывающий):
# каталог данных, префикс меток launchd и bundle id, подстановка в шаблоны launchd/ и Info.plist.
DATA_DIR="${TRANSCRIBER_DATA:-$HOME/Library/Application Support/Transcriber}"

# Префикс: TRANSCRIBER_LABEL_PREFIX > строка LABEL_PREFIX=… в $DATA_DIR/install.env > local.transcriber.
# Из него: bundle id приложения, метки <prefix>.app / <prefix>.worker, UID агрегатного устройства.
# Сменить префикс на живой установке = заново выдать разрешения macOS (микрофон, запись звука,
# универсальный доступ): они привязаны к bundle id.
_label_prefix_from_file() {   # последняя строка [export] LABEL_PREFIX = "…"; пробелы, кавычки и \r снимаются
  if [ -f "$DATA_DIR/install.env" ]; then
    tr -d '\r' < "$DATA_DIR/install.env" \
      | sed -nE 's/^[[:space:]]*(export[[:space:]]+)?LABEL_PREFIX[[:space:]]*=[[:space:]]*//p' \
      | tail -n 1 | tr -d "\"'" | sed -E 's/[[:space:]]+$//'
  fi
}
LABEL_PREFIX="${TRANSCRIBER_LABEL_PREFIX:-$(_label_prefix_from_file)}"
LABEL_PREFIX="${LABEL_PREFIX:-local.transcriber}"
if ! [[ "$LABEL_PREFIX" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]]; then
  echo "LABEL_PREFIX «$LABEL_PREFIX»: нужен обратный домен из букв, цифр и дефисов, например local.transcriber" >&2
  return 1 2>/dev/null || exit 1
fi
for _old in "$HOME"/Library/LaunchAgents/*.transcriber.app.plist "$HOME"/Library/LaunchAgents/*.transcriber.worker.plist; do
  [ -e "$_old" ] || continue
  case "$(basename "$_old")" in "$LABEL_PREFIX".*) continue ;; esac
  echo "внимание: стоит агент $(basename "$_old"), а префикс сейчас $LABEL_PREFIX — новый bundle id потеряет разрешения macOS;" \
       "свой префикс задают LABEL_PREFIX= в $DATA_DIR/install.env, старый агент снимают launchctl bootout" >&2
done

_sed_value() {   # значение для правой части s|…|…|: XML-экранирование & < >, затем экранирование для sed
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[|\\&]/\\&/g'
}
render_template() {   # render_template <шаблон> <куда>
  sed -e "s|__HOME__|$(_sed_value "$HOME")|g" -e "s|__REPO__|$(_sed_value "$ROOT")|g" \
      -e "s|__LABEL_PREFIX__|$LABEL_PREFIX|g" "$1" > "$2"
}
