# SIP voice bots

This project provides two voice bots built on the same SIP, G.711, digest-auth,
and realtime voice bridge. Grok Voice is active by default; GPT Realtime remains
available as a configuration switch:

- `partyline-llm` registers a SIP endpoint and calls the existing `*99` party
  line.
- `spooky-llm` registers as extension `666`, waits for incoming calls, and
  rotates through a shuffled cast of funny supernatural personalities. It uses
  a persistent SIP/TCP connection by default so incoming calls work through NAT
  without an inbound SIP port forward.

It is designed for the topology visible in `SEPD0C78915E83F.cnf.xml`:

- SIP server: `10.13.37.10:5060`
- party-line speed dial: `*99`
- phone codec path: 8 kHz telephone audio

Neither executable modifies the Cisco phone configuration.

## What you need on the SIP server

Create a dedicated SIP account for the bot, for example extension `199`, with a
new password. It must be allowed to call `*99`. Do not register the bridge as
extension `102`; duplicate registration could disrupt that handset.

The bridge assumes the PBX answers `*99` as a conference/party line and does not
send a participant its own conference audio. If it does echo the bot back to
itself, disable that behavior in the conference configuration.

For the spooky bot, provision a separate PJSIP endpoint with auth username
`666`, a new password, and an AOR/contact that allows it to register. Calls to
extension `666` must route to that registered endpoint. The TCP bot accepts up
to `MAX_CONCURRENT_CALLS` simultaneous calls (four by default), with an
independent realtime voice session and RTP stream for every caller. Additional
callers receive SIP Busy until a slot opens.

The included `dialplan.xml` adds an immediate `666` match for the Cisco phones.
Place it in the provisioning server's TFTP root and reload or reboot the phones.
Without that rule, the supplied handset dial plan treats `666` as a catch-all
number and waits on its digit timeout instead of dialing immediately.

## Install

Python 3.12 is recommended. PyVoIP currently depends on Python's `audioop`
module, so this project intentionally requires Python earlier than 3.13.
The checked-in `.python-version` asks uv to use Python 3.12.

```powershell
uv sync
Copy-Item .env.example .env
Copy-Item .env.spooky.example .env.spooky
```

`uv sync` creates or updates `.venv` and installs the default `dev` dependency
group, including pytest. The checked-in `uv.lock` keeps installs reproducible.

Edit `.env` for the party-line bot and `.env.spooky` for the incoming bot. Set
the corresponding SIP passwords and an xAI API key in each file.

Party-line minimum settings:

```dotenv
REALTIME_PROVIDER=xai
XAI_API_KEY=xai-...
SIP_USERNAME=199
SIP_PASSWORD=the-new-bot-extension-password
```

Spooky bot minimum settings:

```dotenv
REALTIME_PROVIDER=xai
XAI_API_KEY=xai-...
SIP_USERNAME=666
SIP_PASSWORD=the-extension-666-password
```

`SIP_LOCAL_IP` is normally detected by asking Windows which local address it
uses to reach the PBX. Set it explicitly if the SIP/SDP logs show `0.0.0.0` or
the wrong interface.

If this runs directly on the PBX machine, keep `SIP_LOCAL_PORT` different from
the PBX listener (the example uses `5062`). Permit inbound and outbound UDP for
that port and for `SIP_RTP_PORT_LOW` through `SIP_RTP_PORT_HIGH`.

## Run

### Party-line bot

Validate configuration without contacting SIP or the voice provider:

```powershell
uv run partyline-llm --check
```

Join the party line and reconnect automatically if the conference or network
drops:

```powershell
uv run partyline-llm
```

For a single attempt while testing:

```powershell
uv run partyline-llm --once
```

### Incoming personality roulette on 666

Validate its separate configuration:

```powershell
uv run spooky-llm --check
```

If `.env.spooky` does not exist yet, `spooky-llm` falls back to `.env`. Your
current `.env` is configured as extension `666`, so the command works directly.

Register extension `666` and wait for calls:

```powershell
uv run spooky-llm
```

Answer one call and exit afterward:

```powershell
uv run spooky-llm --once
```

Both versions can run simultaneously because their example configurations use
different local SIP ports and RTP ranges.

## Call recordings and booth dashboard

Both executables record calls by default and start a small dashboard alongside
the SIP service. Open the spooky-bot dashboard from another device on the booth
LAN at:

```text
http://<raspberry-pi-address>:8080
```

The outgoing party-line bot uses port `8081` in `.env.example`, allowing both
dashboards to run at the same time. The dashboard shows active calls, elapsed
time, the selected personality, completed recordings, and audio players. Click
**Listen live** on an active session to hear a low-latency mixed feed in the
browser. Browsers require that click before they permit audio playback.

Recordings are saved as 8 kHz stereo WAV files under `recordings/`: caller audio
is the left channel and bot audio is the right channel. A JSON metadata file is
written beside each WAV so recordings remain visible after a restart. The
dashboard supports HTTP byte ranges, so completed calls can be played and
seeked without downloading the entire file first.

The dashboard has no login and `WEBUI_HOST=0.0.0.0` makes it reachable from the
local network. Keep it on the booth LAN, or set `WEBUI_HOST=127.0.0.1` if it
should only be available on the Pi itself.

Run the test suite with:

```powershell
uv run pytest -q
```

Set `LOG_LEVEL=DEBUG` for event and transcript diagnostics. API keys and SIP
passwords are never written to logs.

## Useful settings

- `REALTIME_PROVIDER`: `xai` by default; set it to `openai` to restore GPT
  Realtime without changing code.
- `XAI_API_KEY`: required while `REALTIME_PROVIDER=xai`.
- `XAI_REALTIME_MODEL`: defaults to `grok-voice-latest`.
- `XAI_VOICE`: defaults to `eve`. xAI also exposes the current voice catalog at
  `GET https://api.x.ai/v1/tts/voices`.
- `XAI_OUTPUT_GAIN`: playback amplification applied to the bot voice; defaults
  to `2.0` (about +6 dB), with clipping at the telephone codec's limits.
- `SPOOKY_XAI_VOICE`: optional Grok voice override only for `spooky-llm`.
- `XAI_VAD_THRESHOLD`: speech activation threshold from `0.1` to `0.9`;
  defaults to `0.85`.
- `XAI_VAD_PREFIX_PADDING_MS`: audio retained before detected speech; defaults
  to `333`.
- `XAI_VAD_SILENCE_DURATION_MS`: quiet time required to end a turn; defaults
  to `900`.
- `XAI_INSTRUCTIONS`: generic behavior override for the selected bot.
- `XAI_GREETING`: generic greeting override; leave empty for a silent join.
- `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL`, `OPENAI_VOICE`, and the matching
  OpenAI VAD/gain settings remain available when `REALTIME_PROVIDER=openai`.
- `SIP_PARTYLINE`: defaults to `*99`.
- `SIP_TRANSPORT`: `spooky-llm` defaults to `tcp`; the party-line bot defaults
  to `udp`.
- `RECONNECT_SECONDS`: delay before another registration/call attempt.
- `MAX_CONCURRENT_CALLS`: simultaneous incoming TCP calls; defaults to `4` for
  `spooky-llm`.
- `RECORD_CALLS`: records both sides of each call when true; defaults to true.
- `RECORDINGS_DIR`: WAV and JSON storage directory; defaults to `recordings`.
- `WEBUI_ENABLED`: starts the recordings/session dashboard when true.
- `WEBUI_HOST`: dashboard bind address; defaults to `0.0.0.0`.
- `WEBUI_PORT`: dashboard port; the examples use `8080` for `spooky-llm` and
  `8081` for `partyline-llm`.
- `PARTYLINE_REALTIME_INSTRUCTIONS` and `PARTYLINE_REALTIME_GREETING`: override
  the party-line personality without changing the spooky bot.
- `SPOOKY_REALTIME_INSTRUCTIONS`, `SPOOKY_REALTIME_GREETING`, and the
  provider-specific spooky voice setting customize extension 666. The spooky
  executable intentionally ignores generic prompt, greeting, and voice values
  from the party-line `.env`. Built-in personalities are shuffled per call;
  every one is used once before the next shuffled cycle begins.

The audio path is full duplex. The selected provider's server voice detection
decides when someone has finished a turn, and queued bot audio is dropped when
new caller speech begins so interruptions feel natural. A quiet telephone dial
tone plays after the SIP call connects and stops when the provider WebSocket is
ready.

## Adding another version

New personalities are defined as `BotProfile` objects in
`partyline_llm/profiles.py`. A version can reuse either the outbound runner in
`partyline_llm/sip.py` or the incoming runner in `partyline_llm/incoming.py`.
Phone construction, Asterisk digest authentication, codec conversion, and the
Realtime bridge remain shared.

## First-call troubleshooting

- Registration timeout: verify the dedicated extension, secret, PBX ACL, and
  that UDP SIP reaches `10.13.37.10:5060`.
- Call never answers: confirm `*99` is dialable from the bot extension's dial
  context and that the conference returns SIP `200 OK`.
- One-way or no audio: set `SIP_LOCAL_IP` explicitly and open the configured RTP
  range. The advertised SDP address must be reachable from the PBX.
- Choppy audio: run the bridge on the same LAN as the PBX and ensure the RTP
  ports are not already in use.
- The bot talks to itself: configure the PBX conference not to loop a
  participant's transmitted audio back to that same participant.

## Why the bridge uses a Realtime WebSocket

For this private-LAN party line, registering a local endpoint and using a
server-to-server Realtime WebSocket avoids exposing the PBX, hosting a public
call webhook, or changing the existing conference routing.
