# search_travel
skill for discovery and booking trip

Ядро — скилл [`skills/trip-planner`](skills/trip-planner/SKILL.md) для Claude Code: планирует поездку от двери до двери и отвечает JSON. Система — [`system/`](system/): веб-страница и API поверх ядра. План и этапы: [`SYSTEM_PLAN.md`](SYSTEM_PLAN.md).

```sh
python3 -m system.server --demo   # показ без сети и без затрат: данные выдуманы
python3 -m system.server          # настоящее ядро: нужен залогиненный claude и сеть
# → http://127.0.0.1:8000

python3 -m unittest discover -s skills/trip-planner/tests
python3 -m unittest discover -s system/tests -t .
```

Скилл отдельно: `npx skills add https://github.com/all0b0y/search_travel --skill trip-planner`.

Скиллы для работы над репозиторием лежат в `.claude/skills/` и закреплены в `skills-lock.json` (`npx skills experimental_install` восстанавливает их).
