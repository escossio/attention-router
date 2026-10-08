# AGT WhatsApp container boot orchestration

The live runtime uses existing containers and the authenticated profile. This package does not build images, recreate containers, pair, log out, send messages, create VLANs, or publish CDP. It should be installed only after its PR checks pass. The reboot certification is a separate authorized step.

## Boot ownership

`andy-whatsapp-container-runtime.service` owns startup and shutdown order. Docker's `on-failure:5` policy retries a process crash but [does not restore a container when the daemon restarts](https://docs.docker.com/engine/containers/start-containers-automatically/). The unit starts after AppArmor, Docker, network-online, and local mounts. Its bounded retries have a 60-second interval and a three-attempt start limit. It starts only existing containers; a missing container fails closed.

At startup, the controller checks the installed AppArmor profile against the bundled version, loaded enforcement state, Docker, both VLAN parents, the private Compose environment and `config --quiet`, bind mounts/profile writer guard, sandbox settings, no host port bindings, `on-failure:5` policy, and all eight retired units masked. It then starts Browser, waits for health, and checks local CDP, one WhatsApp page, connected authentication state over the existing private CDP bridge, the operational flock, one Chrome writer, Singleton state, IP and forwarding. Only after this gate does it start and verify Transport. Observer follows after Transport. Authentication, QR absence and page counts are verified from Transport and Observer. A failed gate blocks later starts; there is no pairing or profile repair path.

On systemd shutdown, `ExecStop` stops Observer, Transport, then Browser with a 45-second grace period each. `After=docker.service` also places this stop before Docker stops. The service is `oneshot` with `RemainAfterExit=yes`; a repeated `systemctl start` does not cycle healthy containers.

## Install and no-reboot acceptance

On AGT, from the reviewed source checkout:

```sh
sudo ops/whatsapp-container/boot/install.sh
# Existing containers only; docker update does not restart them.
sudo docker update --restart=on-failure:5 andy-whatsapp-browser andy-whatsapp-transport andy-whatsapp-observer
sudo /usr/local/lib/andy-whatsapp-container-runtime/bootstrap.py preflight
sudo systemctl enable --now andy-whatsapp-container-runtime.service
sudo systemctl start andy-whatsapp-container-runtime.service
```

Check `systemctl is-enabled/is-active`, the unit journal's `BOOT_CONVERGENCE=PASS`, `aa-status --json`, SHA-256 of `/etc/apparmor.d/andy-whatsapp-browser`, unchanged container IDs/StartedAt, and the three live health/status sources. The boot host needs the installed `python3-websocket` package for the Browser auth probe; it is already present on AGT. Do not `systemctl restart` on a healthy runtime because `ExecStop` intentionally performs an ordered stop. Do not unload the AppArmor profile from the running Browser; the versioned install performs a safe parser reload. The file in `/etc/apparmor.d/` is loaded again by the standard AppArmor service at boot.

## Rollback

`sudo ops/whatsapp-container/boot/rollback.sh` marks `ExecStop` to leave live containers running, disables/stops only the new unit, and restores `unless-stopped` to the three existing containers. It leaves the AppArmor profile installed because the current Browser requires it. It never unmasks or starts host-native units. Inspect runtime health after rollback. The script does not restore an earlier Compose source; revert the branch change through Git before future Compose reconciliation.

## Reboot certification later

A separate authorized reboot must prove: AppArmor loaded from `/etc/apparmor.d`, unit enabled and `BOOT_CONVERGENCE=PASS`, Browser → Transport → Observer startup timestamps, healthy states, READY/CONNECTED, QR absent, no disconnect increase, one writer, 1/1 pages, private CDP, masked legacy units, and ordered shutdown journal from the previous boot. Stop here until reboot authorization is given.
