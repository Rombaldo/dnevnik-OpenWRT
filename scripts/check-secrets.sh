#!/bin/bash
# Ищет в проекте секреты и личные данные: ключи, токены, cookie входа, номера ученика, файлы с данными.
#   ./scripts/check-secrets.sh staged    - то, что сейчас добавлено в коммит (запускается хуком автоматически)
#   ./scripts/check-secrets.sh tree      - все файлы проекта, которые отслеживает git
#   ./scripts/check-secrets.sh history   - ВСЯ история git (запустить перед первой отправкой на GitHub)
cd "$(dirname "$0")/.." || exit 2
MODE="${1:-staged}"
FAIL=0

# Файлы, которых в git быть не должно ни при каких условиях
NAMES='(^|/)(data/|session\.json$|credentials\.json$|login-state\.json$|ids\.json$|diary\.json$|history\.json$|config\.json$|known_hosts$|\.env$|\.dnevnik-token$|[^/]*router_key[^/]*$|[^/]*\.pem$|[^/]*\.key$|[^/]*\.token$)'
# Явные секреты (ищем везде)
STRONG='BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|[Bb]earer +[A-Za-z0-9._-]{30,}'
# Похоже на данные входа или ученика (в тестах выдуманные значения, там не ищем)
WEAK='(DnevnikAuth_[a-z]|dnevnik_sst|a_r_p_i)["'"'"']? *[:=] *["'"'"']?[A-Za-z0-9%._-]{16,}|personId["'"'"']? *[:=] *["'"'"']?[0-9]{10,}'

report() { echo "СТОП [$1]: $2"; FAIL=1; }

case "$MODE" in
  staged)
    names=$(git diff --cached --name-only --diff-filter=AM)
    diff_all=$(git diff --cached -U0 -- . ':!scripts/check-secrets.sh' | grep -E '^\+[^+]')
    diff_weak=$(git diff --cached -U0 -- . ':!scripts/check-secrets.sh' ':!tests' ':!.githooks' | grep -E '^\+[^+]')
    ;;
  tree)
    names=$(git ls-files)
    diff_all=$(git grep -I -n -E "$STRONG" -- . ':!scripts/check-secrets.sh' 2>/dev/null)
    diff_weak=$(git grep -I -n -E "$WEAK" -- . ':!scripts/check-secrets.sh' ':!tests' ':!.githooks' 2>/dev/null)
    ;;
  history)
    names=$(git log --all --name-only --pretty=format: | sort -u)
    revs=$(git rev-list --all)
    diff_all=$(git grep -I -n -E "$STRONG" $revs -- . ':!scripts/check-secrets.sh' 2>/dev/null)
    diff_weak=$(git grep -I -n -E "$WEAK" $revs -- . ':!scripts/check-secrets.sh' ':!tests' ':!.githooks' 2>/dev/null)
    ;;
  *) echo "Использование: $0 staged|tree|history"; exit 2 ;;
esac

bad=$(echo "$names" | grep -E "$NAMES")
[ -n "$bad" ] && report "файлы" "в git попали файлы с личными данными или ключами:" && echo "$bad" | sed 's/^/    /'
hit=$(echo "$diff_all" | grep -E "$STRONG")
[ -n "$hit" ] && report "ключи и токены" "в тексте найден ключ или токен:" && echo "$hit" | sed -E 's/(.{70}).*/\1…/; s/^/    /' | head -5
hit=$(echo "$diff_weak" | grep -E "$WEAK")
[ -n "$hit" ] && report "вход" "в тексте похоже на cookie входа или номер ученика:" && echo "$hit" | sed -E 's/(.{70}).*/\1…/; s/^/    /' | head -5
[ $FAIL -eq 0 ] && echo "Чисто: секретов и личных данных не найдено ($MODE)."
exit $FAIL
