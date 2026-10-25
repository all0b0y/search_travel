# Sealed benchmark results

One line per run. «Blind» = the first run of a version, before anyone looked at its failures; later runs are «seen».

| Date | Version | Level | Commit | Blind | Result | Report |
|---|---|---|---|---|---|---|
| 2026-10-25 | v1 | 2 | 3f7eea3 | yes | 25/30 cases pass 5 of 5 (125/150 calls). By meaning 28/30: three failures are the same reading in another form (solo gives 1 adult by default, no_hostel already removes dorm beds, children_ages null instead of []). Real misses: dates said to be open became fixed holiday dates; a request for car rental only was read as a trip | v1/reports/20261025-171440-L2-bench-blind.md |
| 2026-10-25 | v1 | 3 | 3f7eea3 | yes | Paid 9/14, hidden requirements 52/70 (74%), every check met 6/14, first recommendation in the first reply 86%, touches to a ready cart 2.75. Bench errors: two after-purchase scenarios stopped at payment (BRIEF lacked `stop_on_paid`); four need transport the mock lacks (a direct Ekaterinburg–Gabala flight, Novosibirsk–Sheregesh and Novosibirsk–Belokurikha flights, Kazan–Anapa). Agent faults in them: claims of changes code did not make, «no tickets» without the connecting or train options, offers of rebuilds that cannot change anything, kitchen and step-free access with no data | v1/reports/20261025-171518-L3-bench-blind.md |
