#!/bin/bash
# Sourced by browser-entrypoint. FD 9 stays open for the browser lifetime.

singleton_block() {
  printf 'SINGLETON_STATE=%s reason=%s\n' "$1" "$2" >&2
  return 1
}

acquire_profile_guard() {
  local profile=$1 host lock_target lock_pid socket_target cookie_target path socket_probe socket_rc
  host=$(hostname)

  # The inode is persistent across Docker restart/recreate and shared by
  # candidates mounting the same profile. Chrome inherits this descriptor.
  exec 9>>"$profile/.andy-browser-writer.lock"
  chmod 0600 "$profile/.andy-browser-writer.lock"
  flock -n 9 || { singleton_block ACTIVE operational_lock_held; return 1; }

  for path in "$profile"/Singleton*; do
    [[ -e "$path" || -L "$path" ]] || continue
    case "${path##*/}" in
      SingletonLock|SingletonSocket|SingletonCookie) ;;
      *) singleton_block AMBIGUOUS_BLOCKED unknown_artifact; return 1 ;;
    esac
  done

  if [[ ! -e "$profile/SingletonLock" && ! -L "$profile/SingletonLock" ]]; then
    if [[ -e "$profile/SingletonSocket" || -L "$profile/SingletonSocket" ||
          -e "$profile/SingletonCookie" || -L "$profile/SingletonCookie" ]]; then
      singleton_block AMBIGUOUS_BLOCKED incomplete_artifacts
      return 1
    fi
    echo 'SINGLETON_STATE=CLEAN'
    return 0
  fi

  if [[ ! -L "$profile/SingletonLock" || ! -L "$profile/SingletonSocket" ||
        ! -L "$profile/SingletonCookie" ]]; then
    singleton_block AMBIGUOUS_BLOCKED unexpected_artifact_type
    return 1
  fi
  lock_target=$(readlink -- "$profile/SingletonLock")
  socket_target=$(readlink -- "$profile/SingletonSocket")
  cookie_target=$(readlink -- "$profile/SingletonCookie")
  if [[ "$lock_target" != "$host"-* ]]; then
    singleton_block AMBIGUOUS_BLOCKED foreign_hostname
    return 1
  fi
  lock_pid=${lock_target#"$host"-}
  if [[ ! "$lock_pid" =~ ^[1-9][0-9]*$ ||
        ! "$socket_target" =~ ^/tmp/com\.google\.Chrome\.[^/]+/SingletonSocket$ ||
        ! "$cookie_target" =~ ^[0-9]+$ ]]; then
    singleton_block AMBIGUOUS_BLOCKED malformed_artifact
    return 1
  fi
  if [[ -e "/proc/$lock_pid" ]]; then
    singleton_block ACTIVE lock_pid_exists
    return 1
  fi
  if [[ -e "$socket_target" ]]; then
    # Docker restart keeps the container's writable /tmp layer. Chrome may
    # leave an unbound socket inode there after its process has exited.
    if [[ ! -S "$socket_target" || ! -O "$socket_target" ||
          ! -O "${socket_target%/*}" ]]; then
      singleton_block AMBIGUOUS_BLOCKED unexpected_socket_target
      return 1
    fi
    if socket_probe=$(timeout 2 socat -u /dev/null "UNIX-CONNECT:$socket_target" 2>&1); then
      singleton_block ACTIVE socket_listener_present
      return 1
    else
      socket_rc=$?
      if [[ "$socket_rc" != 1 || "$socket_probe" != *'Connection refused'* ]]; then
        singleton_block AMBIGUOUS_BLOCKED socket_probe_failed
        return 1
      fi
    fi
  fi

  # Only these three validated links belong to the proven dead writer.
  rm -- "$profile/SingletonLock" "$profile/SingletonSocket" "$profile/SingletonCookie"
  echo 'SINGLETON_STATE=STALE_REMOVED'
}
