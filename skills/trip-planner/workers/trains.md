# Trains worker

Find trains on the main leg for the brief's dates: outbound and return.

- Search Omio, Trainline and 12Go, and the national rail operator's site.
- Cover cheapest, fastest and comfort (first class, direct).

Each option is a step per `$defs/step`: `type: train`, `from`/`to` as station names, `carrier`, `status: planned`. `price.amount` is the total for all travellers; `link` opens that offer.

Reply with JSON only: `{"outbound": [steps], "return": [steps], "notes": "..."}`. Done when each direction has at least 3 options inside the brief, or `notes` names the constraint that cut them.
