# IEC 61850

IEC 61850 GOOSE and MMS, in your Zelos workspace.

- 📡 **Subscribe to a segment**: pick a network interface; every control block publishing on it appears, nothing to connect to
- 🧩 **One event per control block**: `IEC61850/<LD>/LLN0/GO/<gcb>`, the gocbRef as a path, with the header (`stNum`, `sqNum`, `t`, `timeAllowedtoLive`, `simulation`, `confRev`, ...) and every dataset member as a typed field
- 📄 **Named by SCL**: add the station's `.scd`/`.cid` and members read `lnName_doName_daName`; without it they are `d00`, `d01`, ...
- 🔌 **Read an IED over MMS**: reports (buffered and unbuffered) and polled reads land as one event per data object, `IEC61850/<LD>/<LN>/<DO>`, in the same tree
- 🩺 **Health per control block**: per-frame `stnum_gap`, `sqnum_gap`, `tal_expired` flags, so a lost or late publisher shows up as a signal
- 📁 **Open a pcap**: decode `.pcap`/`.pcapng`, plain or gzip/zstd compressed, with no privileges and no NIC
- 🎛️ **Demo**: a bundled one-minute capture, for trying it without a substation
- ⏱️ **One timeline**: trips line up with your Sampled Values, Modbus and sensor data

Read-only by design: this extension never publishes a GOOSE message, and over MMS it writes nothing but the reservation of the report control blocks you name.

## Quick start

[![Install the IEC 61850 extension, pick the interface on the station bus, start it and plot a GOOSE dataset member](assets/howto/quickstart/quickstart.avif)](assets/howto/quickstart/quickstart.mp4)

In the app:

1. **Install** IEC-61850 from the Extensions view
2. **Configure**: add an interface and pick the NIC on the station bus; optionally its SCL file, so members get their IEC 61850 names
3. **Save & Start**
4. **Plot**: control blocks appear under **IEC61850 → logical device → LLN0 → GO**; drag a member into the workspace

Live capture needs a one-time grant on the machine running the agent ([below](#granting-capture-rights)); the **Check permissions** action prints the command. Decoding a file needs nothing.

From the CLI, on the agent that can see the segment:

```bash
zelos extensions install zeloscloud/zelos-extension-iec61850
zelos extensions start zeloscloud.zelos-extension-iec61850 --config '{"interfaces": [{"interface": "eth0"}]}'
```

Decoding a file instead plays it once at the capture's pace, then the extension exits:

```bash
zelos extensions start zeloscloud.zelos-extension-iec61850 --config '{"advanced": {"replay_pcap": "station.pcapng", "replay_scl": "station.scd"}}'
```

Per-frame rows are under **prefix (default IEC61850) → your interface → goose**.

## Granting capture rights

Capturing reads raw frames, which needs one grant per machine. Decoding a file does not.

```bash
uv run zelos-extension-iec61850 check        # when denied, prints the exact command for this machine
sudo /path/to/python -m zelos_packet install-helper
```

- 🔐 **Linux** installs a small privileged helper (`cap_net_raw` only) and a `zelos-packet` group; **macOS** installs a boot-time daemon that puts `/dev/bpf*` in the `access_bpf` group
- 🔁 Log out and back in, then restart the agent: a session's groups are fixed at login
- ✅ `python -m zelos_packet status` says whether capture would work right now
- 🪟 **Windows** needs [Npcap](https://npcap.com) instead (free, default options; 1.82 or newer keeps VLAN tags). On Intel I225-V/I226 NICs set **Packet Priority & VLAN** to **Disabled**, or tagged GOOSE loses its tag. The tree names an interface by its connection name (`Ethernet 5` -> `Ethernet_5`)

## Try it without hardware

A one-minute capture ships with the extension: two publishers, one VLAN-tagged, tripping and resetting.

- **App**: run `IEC-61850/demo_capture_path`, paste the path into **Advanced → Replay PCAP File**, save, start
- **CLI**: `uv run zelos-extension-iec61850 --demo`

Either way it plays once and the extension exits.

## How it listens

GOOSE is layer-2 multicast (EtherType `0x88B8`, tagged or not). The interface is always put in
**promiscuous mode**: without it the NIC drops every frame, so this is not a setting. A capture filter
keeps everything else on the station bus (Sampled Values, IP) out of the trace.

| Event | One row per | Fields |
| --- | --- | --- |
| `<prefix>/<LD>/LLN0/GO/<gcb>` | GOOSE message of that control block (`FULLLD0/LLN0$GO$dcdc` → `IEC61850/FULLLD0/LLN0/GO/dcdc`) | the IEC 61850-8-1 header, named as on the wire: `appid`, `length`, `reserved1`, `reserved2`, `s_bit`, `gocbRef`, `timeAllowedtoLive`, `datSet`, `goID`, `t`, `t_quality`, `stNum`, `sqNum`, `simulation`, `confRev`, `ndsCom`, `numDatSetEntries`; then `src_mac`, `vlan_id`; then one field per dataset member (SCL name, else `d00`, `d01`, ... with the ASN.1 type in the description); then `stnum_gap`, `sqnum_gap`, `tal_expired` |
| `<prefix>/<name>/goose` | GOOSE frame on that interface | `frame_no`, `iface`, the header above, `src_mac`, `dst_mac`, `vlan_id`, `vlan_pcp`, `malformed` |
| `<prefix>/<name>/packets`, `stats` | captured frame, capture tick | as the Packet extension |

- the event time is the capture time of the frame; the publisher's `t` is a field
- a control block without a `goID` is named by its `gocbRef`
- members are typed from the wire (boolean, bit string, integer, float, UTC time, visible string); an SCL refines the type and names them
- `stnum_gap`/`sqnum_gap` are per-frame booleans, true when that counter skipped a value since the previous message; `tal_expired` is true when the previous message's `timeAllowedtoLive` had passed

## Reading an IED over MMS

Add the IED under **MMS Servers** with the report control blocks to enable; `IEC61850/browse` lists what a server has. MMS is TCP, so it needs no capture rights and runs beside GOOSE or alone.

```bash
zelos extensions start zeloscloud.zelos-extension-iec61850 --config '{"servers": [{"host": "192.168.1.10", "reports": ["IED1LD0/LLN0$RP$urcbMeas01"]}]}'
```

| Event | One row per | Fields |
| --- | --- | --- |
| `<prefix>/<LD>/<LN>/<DO>` | report or read that carried the data object | its attributes, path joined by `_` (`stVal`, `mag_f`, `q`, `t`), typed by the server; then `src` (`report` or `read`), `rcb`, `rpt_sqnum`, `reason` |
| `<prefix>/<LD>/LLN0/RP/<name>`, `.../BR/<name>` | report received | `rptID`, `sqNum`, `timeOfEntry`, `entryID`, `confRev`, `bufOvfl`, `datSet`, `n_included`, `reasons` |

- a row holds only what its report carried: a data-change report leaves the other attributes empty
- `reason` is a bitmask: 1 data change, 2 quality change, 4 data update, 8 integrity, 16 general interrogation
- every block is enabled with data change, quality change, a 2 s integrity period and a general interrogation, so the first report carries every member
- `q` and `stVal` of a double point are the bit string as an integer (position: 1 off, 2 on); `t` is ns since the epoch; the event time is the receive time
- a lost connection is retried and the blocks enabled again; a block another client holds is retried until it frees; a block or reference the server does not have stops the extension with the reason
- two servers with the same logical device name are refused

## Settings

| Setting | Default | |
| --- | --- | --- |
| `interfaces[].interface` | – | NIC to subscribe on |
| `interfaces[].name` | interface name | Names this interface's branch in the tree |
| `interfaces[].scl` | – | SCD/CID file to name and type dataset members by |
| `servers[].host`, `port` | –, `102` | IED to read over MMS |
| `servers[].name` | host | Names the server in logs and status |
| `servers[].reports` | – | Report control blocks to reserve and enable: `LD/LLN0$RP$name01`, `LD/LLN0$BR$name01` |
| `servers[].reads` | – | `{ref, interval_s}`: a data object or attribute read on an interval, `LD/LN.DO[FC]` |
| `advanced.prefix` | `IEC61850` | Trace source, one tree for the station: data objects at `<prefix>/<LD>/<LN>/<DO>`, control blocks at `<prefix>/<LD>/LLN0/GO\|RP\|BR/<name>`, per-frame rows at `<prefix>/<name>/goose` |
| `advanced.replay_pcap` | – | Decode this `.pcap`/`.pcapng` (plain or `.gz`/`.zst`) instead of capturing; plays once, then exits. `interfaces` is ignored |
| `advanced.replay_scl` | – | SCD/CID file for the replayed capture's publishers |
| `advanced.log_level` | `INFO` | |

## Actions

| Action | |
| --- | --- |
| `IEC-61850/list_interfaces` | NICs on the machine running the agent |
| `IEC-61850/check_permissions` | Can we capture? If not, the exact fix |
| `IEC-61850/list_gocbs` | Every control block seen: goID, gocbRef, datSet, APPID, publisher, stNum/sqNum/confRev, members, alive |
| `IEC-61850/get_status` | Per-interface capture and decoder counters (drops, malformed, gaps, expired); per MMS server its state, blocks, counts and last error |
| `IEC-61850/browse` | A server's logical devices, data sets and report control blocks, over MMS. Runs without the extension started |
| `IEC-61850/convert_pcap` | Convert a pcap to `.trz`, optionally with an SCL. Runs without the extension started |
| `IEC-61850/demo_capture_path` | Path of the bundled demo capture, for `advanced.replay_pcap`. Runs without the extension started |

`list_gocbs` and `get_status` need the extension running; the rest also run with it stopped.

```bash
zelos actions execute IEC-61850/check_permissions
```

A capture that ends while running is logged and reported by `get_status`; when the last one ends, the extension logs the reason and exits with an error. Restart it to capture again.

## CLI

```bash
uv run zelos-extension-iec61850 interfaces                       # list NICs
uv run zelos-extension-iec61850 check                            # probe permissions
uv run zelos-extension-iec61850 capture eth0 --scl station.scd   # subscribe, no app config
uv run zelos-extension-iec61850 replay station.pcapng            # stream a file to the agent once, paced
uv run zelos-extension-iec61850 convert station.pcapng.gz        # write station.trz, no agent needed
uv run zelos-extension-iec61850 --demo                           # the bundled capture, played once
uv run zelos-extension-iec61850 browse 192.168.1.10              # an IED's data sets and report blocks
uv run zelos-extension-iec61850 mms 192.168.1.10 --report 'IED1LD0/LLN0$RP$urcbMeas01' --read 'IED1LD0/MMXU1.TotW[MX]=0.5'
```

`capture`, `replay` and `convert` take `--scl` and `--prefix`.

## Links

- [Documentation](https://docs.zeloscloud.io)
- [Issues](https://github.com/zeloscloud/zelos-extension-iec61850/issues)

MIT. See [LICENSE](LICENSE).
