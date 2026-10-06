---
name: search-travel
description: Trip search, from idea to booking link. Use when the user wants ideas for where to go, or wants flights, stays, or a whole trip found and compared.
---

A trip runs on a **brief**: the fixed facts every search is filtered against. Two branches share it: **discovery** (no destination yet) and **search** (destination known). Discovery ends where search begins.

## 1. Pin the brief

Fields: origin, dates or a flexibility window, travellers (adults, children with ages), total budget and currency, hard constraints (direct flights only, pets, accessibility, visa limits).

These are the user's decisions: ask for every field the request leaves open, in one message, and wait. Done when every field holds a value or the user has marked it _flexible_.

## 2. Discovery (no destination)

Shortlist 3–5 destinations that fit the brief. For each: why it fits, the season during the travel dates, a rough total cost for all travellers, and the entry requirements for the travellers' passports. The user picks one, which moves the trip to search.

## 3. Search

Find options for each leg (outbound, stay, return) on primary sources: airline, hotel, and aggregator sites. Every option carries:

- the total price for all travellers, taxes and fees included
- exact dates and times, and for flights the stops and baggage allowance
- the direct link
- the time you checked the price

Prices are point-in-time quotes; the checked-at time is what lets the user judge staleness. Done when each leg has at least 3 options inside the brief, or you report that fewer exist and which constraint is cutting them.

## 4. Compare

Lay the options out in one table per leg, then recommend one combination and give the trade-off it makes against the runner-up.

## 5. Hand off

Booking ends at the checkout page: give the user the direct link and the exact selections to enter (fare class, room type, traveller names as on passports). The user enters payment and confirms.
