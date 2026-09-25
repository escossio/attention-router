#!/bin/sh
set -eu

ACTION="${1:-check}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="${NATIVE_OTEL_EDGE_ENV_FILE:-/etc/attention-router/native-otel-edge.env}"
SITE_NAME="attention-native-otel-edge.conf"
SITE_AVAILABLE="/etc/apache2/sites-available/$SITE_NAME"
SITE_ENABLED="/etc/apache2/sites-enabled/$SITE_NAME"
TEMPLATE="$SCRIPT_DIR/apache-site.conf.template"
RENDERER="$SCRIPT_DIR/render.py"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing required command: $1" >&2
        exit 1
    }
}

need_root() {
    [ "$(id -u)" -eq 0 ] || {
        echo "action '$ACTION' requires root" >&2
        exit 1
    }
}

check_modules() {
    modules="$(apache2ctl -M 2>/dev/null)"
    for module in proxy_module proxy_http_module authz_core_module authz_host_module; do
        echo "$modules" | grep -q "$module" || {
            echo "required Apache module not loaded: $module" >&2
            exit 1
        }
    done
}

render_to() {
    python3 "$RENDERER"         --env-file "$ENV_FILE"         --template "$TEMPLATE"         --output "$1"
}

need python3
need apache2ctl

tmp="$(mktemp)"
backup="$(mktemp)"
trap 'rm -f "$tmp" "$backup"' EXIT HUP INT TERM

case "$ACTION" in
    check)
        [ -r "$ENV_FILE" ] || {
            echo "host-local env file not readable: $ENV_FILE" >&2
            exit 1
        }
        check_modules
        render_to "$tmp"
        apache2ctl configtest >/dev/null
        echo "native OTLP edge preflight PASS"
        ;;

    install)
        need_root
        need systemctl
        [ -r "$ENV_FILE" ] || {
            echo "host-local env file not readable: $ENV_FILE" >&2
            exit 1
        }
        check_modules
        render_to "$tmp"

        had_available=0
        had_enabled=0
        if [ -e "$SITE_AVAILABLE" ]; then
            cp -a "$SITE_AVAILABLE" "$backup"
            had_available=1
        fi
        if [ -L "$SITE_ENABLED" ] || [ -e "$SITE_ENABLED" ]; then
            had_enabled=1
        fi

        install -m 0644 "$tmp" "$SITE_AVAILABLE"
        ln -sfn "../sites-available/$SITE_NAME" "$SITE_ENABLED"

        if ! apache2ctl configtest; then
            if [ "$had_available" -eq 1 ]; then
                cp -a "$backup" "$SITE_AVAILABLE"
            else
                rm -f "$SITE_AVAILABLE"
            fi
            if [ "$had_enabled" -eq 0 ]; then
                rm -f "$SITE_ENABLED"
            fi
            apache2ctl configtest >/dev/null 2>&1 || true
            echo "Apache config rejected; previous state restored" >&2
            exit 1
        fi

        systemctl reload apache2
        echo "native OTLP edge installed"
        ;;

    uninstall)
        need_root
        need systemctl

        had_enabled=0
        if [ -L "$SITE_ENABLED" ] || [ -e "$SITE_ENABLED" ]; then
            had_enabled=1
            rm -f "$SITE_ENABLED"
        fi

        if ! apache2ctl configtest; then
            if [ "$had_enabled" -eq 1 ]; then
                ln -sfn "../sites-available/$SITE_NAME" "$SITE_ENABLED"
            fi
            echo "Apache config invalid without the native OTLP edge; uninstall aborted" >&2
            exit 1
        fi

        systemctl reload apache2
        rm -f "$SITE_AVAILABLE"
        echo "native OTLP edge uninstalled"
        ;;

    *)
        echo "usage: $0 check|install|uninstall" >&2
        exit 2
        ;;
esac
