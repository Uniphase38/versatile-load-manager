#!/usr/bin/env python3
"""Relevé léger des constantes d'un Cerbo GX depuis un PC Linux.

Usage (sur le PC Mint, sans installation sur le Cerbo) :
    python3 cerbo_vitals.py --hours 24
    python3 cerbo_vitals.py --host venus.local --interval 60 --hours 0

SSH root doit déjà fonctionner. Le Cerbo ne reçoit qu'un shell en lecture seule
qui dort entre les échantillons ; le CSV est écrit exclusivement sur ce PC.
Arrêter avec Ctrl+C. Aucun accès Modbus, D-Bus ou Shelly.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REMOTE_SCRIPT = r"""
#!/bin/sh
INTERVAL=$1
cores=0
while read -r label rest; do
    case "$label" in cpu[0-9]*) cores=$((cores + 1));; esac
done < /proc/stat
[ "$cores" -gt 0 ] || cores=1

while :; do
    IFS= read -r cpu_stat < /proc/stat
    IFS= read -r load_avg < /proc/loadavg
    IFS= read -r uptime < /proc/uptime
    boot_id=
    [ ! -r /proc/sys/kernel/random/boot_id ] || \
        IFS= read -r boot_id < /proc/sys/kernel/random/boot_id

    mem_total= mem_available= mem_free= mem_buffers= mem_cached=
    swap_total= swap_free=
    while read -r key value unit rest; do
        case "$key" in
            MemTotal:) mem_total=$value;;
            MemAvailable:) mem_available=$value;;
            MemFree:) mem_free=$value;;
            Buffers:) mem_buffers=$value;;
            Cached:) mem_cached=$value;;
            SwapTotal:) swap_total=$value;;
            SwapFree:) swap_free=$value;;
        esac
    done < /proc/meminfo

    temp_mc= temp_type=
    for zone in /sys/class/thermal/thermal_zone*; do
        [ -r "$zone/temp" ] || continue
        IFS= read -r candidate < "$zone/temp" || true
        case "$candidate" in ''|*[!0-9]*) continue;; esac
        kind=
        [ ! -r "$zone/type" ] || IFS= read -r kind < "$zone/type" || true
        if [ -z "$temp_mc" ]; then
            temp_mc=$candidate
            temp_type=$kind
        fi
        case "$kind" in
            *cpu*|*CPU*|*soc*|*SoC*)
                temp_mc=$candidate
                temp_type=$kind
                break;;
        esac
    done

    data_free_kb=$(df -Pk /data 2>/dev/null | awk 'NR==2 {print $4}')

    vlm_pid= vlm_stat= vlm_rss_kb= vlm_threads=
    if [ -r /var/run/versatile-load-manager.lock ]; then
        IFS= read -r candidate < /var/run/versatile-load-manager.lock || true
        case "$candidate" in
            ''|*[!0-9]*) ;;
            *)
                if [ -r "/proc/$candidate/stat" ]; then
                    cmdline=$(tr '\000' ' ' < "/proc/$candidate/cmdline" 2>/dev/null)
                    case "$cmdline" in
                        *versatile_load_manager*run*)
                            vlm_pid=$candidate
                            IFS= read -r vlm_stat < "/proc/$candidate/stat" || true
                            while read -r key value unit rest; do
                                case "$key" in
                                    VmRSS:) vlm_rss_kb=$value;;
                                    Threads:) vlm_threads=$value;;
                                esac
                            done < "/proc/$candidate/status";;
                    esac
                fi;;
        esac
    fi

    printf '@@BEGIN@@\n'
    printf 'epoch\t%s\n' "$(date +%s)"
    printf 'boot_id\t%s\n' "$boot_id"
    printf 'cores\t%s\n' "$cores"
    printf 'cpu_stat\t%s\n' "$cpu_stat"
    printf 'load_avg\t%s\n' "$load_avg"
    printf 'uptime\t%s\n' "$uptime"
    printf 'mem_total\t%s\n' "$mem_total"
    printf 'mem_available\t%s\n' "$mem_available"
    printf 'mem_free\t%s\n' "$mem_free"
    printf 'mem_buffers\t%s\n' "$mem_buffers"
    printf 'mem_cached\t%s\n' "$mem_cached"
    printf 'swap_total\t%s\n' "$swap_total"
    printf 'swap_free\t%s\n' "$swap_free"
    printf 'temp_mc\t%s\n' "$temp_mc"
    printf 'temp_type\t%s\n' "$temp_type"
    printf 'data_free_kb\t%s\n' "$data_free_kb"
    printf 'vlm_pid\t%s\n' "$vlm_pid"
    printf 'vlm_stat\t%s\n' "$vlm_stat"
    printf 'vlm_rss_kb\t%s\n' "$vlm_rss_kb"
    printf 'vlm_threads\t%s\n' "$vlm_threads"
    printf '@@END@@\n'
    sleep "$INTERVAL" || exit 0
done
"""


COLUMNS = (
    "time_local", "event", "cerbo_epoch", "boot_id", "uptime_s",
    "cpu_busy_pct", "cpu_iowait_pct", "load_1m", "load_5m", "load_15m",
    "cpu_cores", "mem_available_mb", "mem_total_mb", "mem_available_estimated",
    "swap_used_mb", "temperature_c", "temperature_type", "data_free_mb",
    "vlm_running", "vlm_pid", "vlm_cpu_pct_one_core", "vlm_rss_mb", "vlm_threads",
)


def number(value: str | None, default=None):
    try:
        return float(value) if value not in (None, "") else default
    except (ValueError, TypeError):
        return default


def first(value: str | None):
    return (value or "").split()[0] if value and value.split() else ""


def cpu_counters(value: str):
    parts = value.split()
    if len(parts) < 9 or parts[0] != "cpu":
        return None
    try:
        ticks = [int(part) for part in parts[1:9]]
    except ValueError:
        return None
    return sum(ticks), ticks[3] + ticks[4], ticks[4]


def vlm_counters(value: str):
    # /proc/PID/stat: le nom entre parenthèses peut lui-même contenir des espaces.
    closing = value.rfind(")")
    if closing < 0:
        return None
    fields = value[closing + 1:].split()  # fields[0] est l'état, champ n° 3.
    if len(fields) < 20:
        return None
    try:
        return int(fields[11]) + int(fields[12]), int(fields[19])
    except ValueError:
        return None


def mb(kb):
    value = number(kb)
    return round(value / 1024, 1) if value is not None else ""


class Sampler:
    def __init__(self):
        self.previous_cpu = None
        self.previous_vlm = None
        self.previous_boot = ""
        self.previous_uptime = None

    def row(self, values: dict[str, str]):
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        uptime = number(first(values.get("uptime")))
        boot = values.get("boot_id", "")
        reboot = bool(
            (self.previous_boot and boot and boot != self.previous_boot)
            or (self.previous_uptime is not None and uptime is not None
                and uptime < self.previous_uptime)
        )
        if reboot:
            self.previous_cpu = self.previous_vlm = None

        load = (values.get("load_avg") or "").split()
        cpu = cpu_counters(values.get("cpu_stat", ""))
        busy = iowait = vlm_cpu = ""
        cores = max(1, int(number(values.get("cores"), 1)))
        if cpu and self.previous_cpu:
            elapsed = cpu[0] - self.previous_cpu[0]
            if elapsed > 0:
                busy = round(100 * (elapsed - (cpu[1] - self.previous_cpu[1])) / elapsed, 1)
                iowait = round(100 * (cpu[2] - self.previous_cpu[2]) / elapsed, 1)
        self.previous_cpu = cpu

        pid = values.get("vlm_pid", "")
        process = vlm_counters(values.get("vlm_stat", ""))
        if cpu and process and self.previous_vlm:
            prev_pid, prev_start, prev_ticks, prev_total = self.previous_vlm
            elapsed = cpu[0] - prev_total
            if pid == prev_pid and process[1] == prev_start and elapsed > 0:
                vlm_cpu = round(100 * cores * (process[0] - prev_ticks) / elapsed, 2)
        self.previous_vlm = (pid, process[1], process[0], cpu[0]) if pid and process and cpu else None

        available = values.get("mem_available", "")
        estimated = "0"
        if not available:
            available = str(sum(int(number(values.get(k), 0)) for k in
                                ("mem_free", "mem_buffers", "mem_cached")))
            estimated = "1"
        swap_total = number(values.get("swap_total"))
        swap_free = number(values.get("swap_free"))
        temp_mc = number(values.get("temp_mc"))

        self.previous_boot, self.previous_uptime = boot, uptime
        return {
            "time_local": now,
            "event": "reboot" if reboot else "sample",
            "cerbo_epoch": values.get("epoch", ""),
            "boot_id": boot,
            "uptime_s": round(uptime, 1) if uptime is not None else "",
            "cpu_busy_pct": busy,
            "cpu_iowait_pct": iowait,
            "load_1m": load[0] if len(load) >= 1 else "",
            "load_5m": load[1] if len(load) >= 2 else "",
            "load_15m": load[2] if len(load) >= 3 else "",
            "cpu_cores": cores,
            "mem_available_mb": mb(available),
            "mem_total_mb": mb(values.get("mem_total")),
            "mem_available_estimated": estimated,
            "swap_used_mb": mb(swap_total - swap_free) if swap_total is not None and swap_free is not None else "",
            "temperature_c": round(temp_mc / 1000, 1) if temp_mc is not None else "",
            "temperature_type": values.get("temp_type", ""),
            "data_free_mb": mb(values.get("data_free_kb")),
            "vlm_running": "1" if pid and process else "0",
            "vlm_pid": pid if pid and process else "",
            "vlm_cpu_pct_one_core": vlm_cpu,
            "vlm_rss_mb": mb(values.get("vlm_rss_kb")),
            "vlm_threads": values.get("vlm_threads", ""),
        }


def run(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", args.host):
        raise SystemExit("Nom/IP Cerbo invalide")
    if args.interval < 30 or args.interval > 3600:
        raise SystemExit("--interval doit être entre 30 et 3600 secondes")
    if args.hours < 0 or not 1 <= args.ssh_port <= 65535:
        raise SystemExit("--hours doit être positif et le port SSH entre 1 et 65535")

    output = args.output or Path("cerbo_vitals_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
    deadline = time.monotonic() + args.hours * 3600 if args.hours else None
    sampler = Sampler()
    count = 0
    print(f"Relevé de root@{args.host} toutes les {args.interval} s → {output}", flush=True)
    with output.open("x", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=COLUMNS)
        writer.writeheader()
        destination.flush()
        while deadline is None or time.monotonic() < deadline:
            command = [
                "ssh", "-T", "-p", str(args.ssh_port),
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=2", f"root@{args.host}",
                "sh", "-s", "--", str(args.interval),
            ]
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, text=True, bufsize=1,
            )
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(REMOTE_SCRIPT)
                process.stdin.close()
                current = None
                received = 0
                for raw in process.stdout:
                    line = raw.rstrip("\r\n")
                    if line == "@@BEGIN@@":
                        current = {}
                    elif line == "@@END@@" and current is not None:
                        row = sampler.row(current)
                        writer.writerow(row)
                        destination.flush()
                        count += 1
                        received += 1
                        print(f"{row['time_local']}  CPU {row['cpu_busy_pct']} %  "
                              f"RAM dispo {row['mem_available_mb']} Mio  "
                              f"VLM {'OK' if row['vlm_running'] == '1' else 'ABSENT'}"
                              f"{'  REDÉMARRAGE' if row['event'] == 'reboot' else ''}", flush=True)
                        current = None
                        if deadline is not None and time.monotonic() >= deadline:
                            break
                    elif current is not None and "\t" in line:
                        key, value = line.split("\t", 1)
                        current[key] = value
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            except KeyboardInterrupt:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                print(f"Arrêt demandé. {count} mesures dans {output}")
                return
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout:
                    process.stdout.close()

            if deadline is not None and time.monotonic() >= deadline:
                break
            if received == 0 and count == 0:
                raise SystemExit("SSH impossible ou aucune mesure reçue ; tester : ssh root@" + args.host)
            writer.writerow({"time_local": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "event": "ssh_lost"})
            destination.flush()
            sampler.previous_cpu = sampler.previous_vlm = None
            print("Connexion perdue ; nouvelle tentative dans 15 s…", file=sys.stderr, flush=True)
            time.sleep(15)
    print(f"Terminé : {count} mesures dans {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="venus.local", help="IP ou nom du Cerbo (défaut : venus.local)")
    parser.add_argument("--ssh-port", type=int, default=22, help="port SSH (défaut : 22)")
    parser.add_argument("--interval", type=int, default=60, help="intervalle en secondes (défaut : 60 ; min : 30)")
    parser.add_argument("--hours", type=float, default=24, help="durée en heures (défaut : 24 ; 0 : jusqu'à Ctrl+C)")
    parser.add_argument("--output", type=Path, help="fichier CSV local (créé sans écraser un fichier existant)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
