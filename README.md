# search_travel
skill for discovery and booking trip

Ядро — скилл [`skills/trip-planner`](skills/trip-planner/SKILL.md) для Claude Code: планирует поездку от двери до двери и отвечает JSON для системы. План и этапы: [`SYSTEM_PLAN.md`](SYSTEM_PLAN.md).

```sh
npx skills add https://github.com/all0b0y/search_travel --skill trip-planner
python3 -m unittest discover -s skills/trip-planner/tests
```

Скиллы для работы над репозиторием лежат в `.claude/skills/` и закреплены в `skills-lock.json` (`npx skills experimental_install` восстанавливает их).
