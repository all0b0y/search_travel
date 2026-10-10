# Sources

You were given the providers of your category in the user's order. Use them in that order, each the way its `access` says:

- `mcp`: its MCP server's tools.
- `public_api` / `api_key`: HTTP calls from Bash to `url`; with a key, put `$<env>` in the header and keep the value inside the variable.
- `site_only`: web search restricted to its site; `link` is that site's page for the offer.

With no provider, or when the providers return nothing, search the web: airline, hotel, operator and aggregator pages. A price read from a page rather than a live API or MCP answer is marked `estimate: true`.

Each `link` opens the offer at the provider it came from, with dates and travellers filled in where the site allows.
