// Letting a generated app use real data.
//
// The model can open a reply with `#fetch <url>`, which means "I cannot answer this
// without data". The app fetches it and asks again with the response attached. That
// costs an extra round trip, so it only happens when the model asks -- a page of
// invented cat breeds needs no network, a page of today's weather does.
//
// The request goes through this app's own server where there is one, because the
// generated page is sandboxed in an iframe and CORS stops it calling most APIs itself.
// Served as static files there is no server to ask, so it tries the API directly and
// takes what it can get.
// Advertised to the model in the prompt as examples, not as a limit: any URL it names is
// fetched. These are public, key-free, and stable enough to name in a prompt that is not
// going to be revised often.
export const KNOWN_APIS = [
    ["https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.13&current=temperature_2m,weather_code",
        "weather now and forecast, by latitude/longitude, no key"],
    ["https://restcountries.com/v3.1/name/{name}", "countries: capital, population, flags, currencies"],
    ["https://hacker-news.firebaseio.com/v0/topstories.json", "Hacker News top story ids; then item/{id}.json"],
    ["https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", "crypto prices"],
    ["https://en.wikipedia.org/api/rest_v1/page/summary/{title}", "Wikipedia article summary"],
    ["https://pokeapi.co/api/v2/pokemon/{name}", "Pokemon details"],
    ["https://api.tvmaze.com/search/shows?q={query}", "TV shows"],
    ["https://api.github.com/repos/{owner}/{repo}", "GitHub repository facts"],
    ["https://datausa.io/api/data?drilldowns=Nation&measures=Population", "US population by year"],
    ["https://api.openbrewerydb.org/v1/breweries?by_city={city}", "breweries by city"],
];
export function apiCatalogue() {
    return KNOWN_APIS.map(([url, what]) => `  ${url}\n    ${what}`).join("\n");
}
const MAX_BODY_CHARS = 2500;
const MAX_ARRAY_ITEMS = 8;
/**
 * Shrink a response to something worth putting in a prompt.
 *
 * API responses are mostly repetition and fields nobody asked for, and every character
 * is paid for on a local model. Long arrays are the worst of it: the first few entries
 * teach the shape and the rest only cost tokens -- the same trade the screen compaction
 * makes.
 */
export function compactData(value, depth = 0) {
    if (Array.isArray(value)) {
        const head = value.slice(0, MAX_ARRAY_ITEMS).map((v) => compactData(v, depth + 1));
        if (value.length > MAX_ARRAY_ITEMS)
            head.push(`…${value.length - MAX_ARRAY_ITEMS} more`);
        return head;
    }
    if (value && typeof value === "object") {
        return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, compactData(v, depth + 1)]));
    }
    if (typeof value === "string" && value.length > 300)
        return value.slice(0, 300) + "…";
    return value;
}
function summarise(body) {
    try {
        const compacted = compactData(JSON.parse(body));
        const text = JSON.stringify(compacted, null, 1);
        return text.length > MAX_BODY_CHARS ? text.slice(0, MAX_BODY_CHARS) + "\n…truncated" : text;
    }
    catch {
        return body.length > MAX_BODY_CHARS ? body.slice(0, MAX_BODY_CHARS) + "…truncated" : body;
    }
}
/** Fetch `url` and return a block of text to append to the prompt. */
export async function fetchForPrompt(url) {
    let result;
    try {
        const viaServer = await fetch(`fetch?url=${encodeURIComponent(url)}`);
        result = viaServer.ok ? await viaServer.json() : null;
    }
    catch {
        result = null;
    }
    if (!result) {
        // No server to ask -- running as static files. Plenty of these APIs allow any origin,
        // so it is worth trying, and a failure here is reported to the model rather than
        // hidden: told the data is unavailable it writes a page that says so, which is much
        // better than one that invents the numbers.
        try {
            const direct = await fetch(url, { headers: { Accept: "application/json" } });
            result = { url, status: direct.status, body: await direct.text() };
        }
        catch (e) {
            result = { url, error: `Could not fetch it from the browser either (${e.message}).` };
        }
    }
    if (result.error)
        return `DATA FROM ${url}:\n(unavailable: ${result.error})`;
    return `DATA FROM ${url} (status ${result.status}):\n${summarise(result.body ?? "")}`;
}
