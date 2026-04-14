# qtcnc — remote GUI over TCP

`qtcnc` cleanly separates the daemon (`qtcnc-serverd`, which owns the
LinuxCNC NML/HAL interface) from the GUI client (`qtcnc`, which is the
actual Qt window). The two talk over ZMQ. By default they use a Unix
domain socket (`ipc://`) for zero-latency local operation, but the same
protocol runs unmodified over `tcp://`, letting an operator run the GUI
on a different machine from the control hardware.

## When to use it

* Workshop layout where the control PC is inside the enclosure and the
  operator sits at a workstation on the network.
* Headless control PC driving servos, with the UI on a tablet or laptop.
* Running multiple screens simultaneously — a shop-floor dashboard and
  an engineer's workstation can subscribe to the same daemon.

## Basic setup — no encryption

On the machine connected to the control hardware:

```
qtcnc-launch --bind tcp://0.0.0.0:5570 /path/to/config.ini
```

`--bind` accepts any ZMQ endpoint string. `tcp://0.0.0.0:5570` listens
on all interfaces; `tcp://10.0.0.5:5570` would bind only that address.
`qtcnc-serverd` uses `port` for the PUB socket and `port + 1` for the
REP socket, so port 5571 must also be free.

On the operator workstation:

```
qtcnc --screen standard --server-endpoint tcp://control-pc:5570
```

`tcp://control-pc:5570` must resolve and route to the daemon's PUB port.
The client will HELLO, pull a snapshot, and bring up the standard
screen.

## With encryption — ZMQ CURVE

Unencrypted TCP is fine on a trusted wired LAN. For anything beyond
that — wireless, multi-tenant networks, or crossing a VPN — use CURVE.

### Generate keys

Each side needs its own keypair. Use the helper that ships with qtcnc:

```
# On the control PC
qtcnc-gen-keys ~/qtcnc-keys/server

# On the operator workstation
qtcnc-gen-keys ~/qtcnc-keys/client
```

Each run creates `NAME.key` (public) and `NAME.key_secret` (secret).
Never copy `*.key_secret` off the machine that generated it.

### Authorize the client on the server

Copy the client's public key (`client.key`, not `client.key_secret`)
into the daemon's authorized directory:

```
mkdir -p ~/qtcnc-keys/authorized
scp operator@workstation:~/qtcnc-keys/client.key \
    ~/qtcnc-keys/authorized/
```

### Start the daemon with CURVE

```
qtcnc-launch --bind tcp://0.0.0.0:5570 /path/to/config.ini \
    -- \
    --curve-server-key ~/qtcnc-keys/server.key_secret \
    --authorized-clients-dir ~/qtcnc-keys/authorized
```

(Any arguments after the `--` are passed straight through to
`qtcnc-serverd`.)

### Start the client with CURVE

The client needs three pieces of key material: its own secret key, its
own public key, and the server's public key. `--curve-client-key` takes
the secret-key file (which also carries the matching public), and
`--curve-server-key` takes the server's public-key file.

```
qtcnc --screen standard \
    --server-endpoint tcp://control-pc:5570 \
    --curve-server-key ~/qtcnc-keys/server.key \
    --curve-client-key ~/qtcnc-keys/client.key_secret
```

If the keys don't match or the client isn't in the authorized dir, ZMQ
drops the connection silently — watch the daemon log for auth failures.

## Troubleshooting

**Client hangs on HELLO**: daemon is not reachable. Check firewalls,
verify both PUB and REP ports (5570 and 5571) are open, try
`telnet control-pc 5571` to confirm the REP socket is accepting.

**Client connects but sees no state updates**: SUB joined late and
missed the initial snapshot. The daemon re-publishes a baseline
snapshot every 30 seconds, so give it a minute. If updates still don't
arrive, bump the daemon's HWM (`--hwm`) — a slow wireless link can
overflow the default queue.

**"invalid curve key" in the daemon log**: the `--curve-server-key`
file on the daemon side must be the `.key_secret` (private) file, not
the `.key` (public). Opposite on the client — `--curve-server-key` is
the server's *public* key.

**Daemon restart drops the client**: the Reconnector catches the
disconnect and retries with exponential backoff (1s, 2s, 4s, … 10s).
If the daemon comes back with a new `daemon_instance_id` the client
fires `daemon_restarted` and redeclares HAL pins.

## Security notes

* CURVE provides both authentication and encryption in one layer.
* qtcnc does not ship a central key rotation scheme. If a client key
  is compromised, remove it from the authorized directory and restart
  the daemon.
* `--authorized-clients-dir` defaults to allowing any CURVE client if
  unset (encryption only, no authentication). Set it to a real
  directory in production.
* Unencrypted TCP (`--bind tcp://...` without CURVE) is acceptable on
  an air-gapped wired network. Do not use it over wireless.
