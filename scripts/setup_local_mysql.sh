#!/usr/bin/env bash
# Bring up a private MySQL 8.4 under a writable directory, no root needed.
#
# For a server where only one volume is writable and there is no package
# manager access: conda supplies the binaries, and every piece of runtime
# state (datadir, socket, pid, log) lives under one base directory.
#
#   bash scripts/setup_local_mysql.sh /mnt/fv/mysql
#
# Re-running is safe: an existing datadir is reused, not re-initialised.
set -euo pipefail

BASE="${1:-/mnt/fv/mysql}"
PORT="${FV_MYSQL_PORT:-3306}"
DB="${FV_MYSQL_DATABASE:-finevision}"
USER_NAME="${FV_MYSQL_USER:-fv}"
PASSWORD="${FV_MYSQL_PASSWORD:-}"
ENV_PREFIX="${FV_MYSQL_ENV:-${BASE}/env}"

if [ -z "${PASSWORD}" ]; then
  echo "set FV_MYSQL_PASSWORD first, e.g.  export FV_MYSQL_PASSWORD='...'" >&2
  exit 2
fi

DATADIR="${BASE}/data"
SOCKET="${BASE}/mysql.sock"
PIDFILE="${BASE}/mysqld.pid"
LOGFILE="${BASE}/mysqld.log"
CONFFILE="${BASE}/my.cnf"
mkdir -p "${BASE}"

# --- binaries ---------------------------------------------------------------
if [ ! -x "${ENV_PREFIX}/bin/mysqld" ] || [ ! -x "${ENV_PREFIX}/bin/mysql" ]; then
  echo "== installing mysql-server 8.4 into ${ENV_PREFIX}"
  command -v conda >/dev/null 2>&1 || { echo "conda not found on PATH" >&2; exit 1; }
  export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-${BASE}/pkgs}"
  # mysql-server ships only the daemon; the `mysql` client is a separate package.
  conda create -y -p "${ENV_PREFIX}" -c conda-forge "mysql-server=8.4.2" "mysql-client=8.4.2"
fi
MYSQLD="${ENV_PREFIX}/bin/mysqld"
MYSQL="${ENV_PREFIX}/bin/mysql"

# --- config -----------------------------------------------------------------
# Written every run and passed as --defaults-file so that ONLY this file is
# read. Without it mysqld also picks up /etc/my.cnf, and a leftover from
# another distribution's MySQL or MariaDB kills startup with errors like
# "unknown variable 'expire_logs_days'" that have nothing to do with us.
# mysqld refuses to run as root unless the account is named explicitly, and
# on these boxes you usually are root.
RUN_AS="$(id -un)"
cat > "${CONFFILE}" <<CNF
[mysqld]
user         = ${RUN_AS}
basedir      = ${ENV_PREFIX}
datadir      = ${DATADIR}
socket       = ${SOCKET}
port         = ${PORT}
pid-file     = ${PIDFILE}
log-error    = ${LOGFILE}
bind-address = 127.0.0.1
mysqlx       = OFF
character-set-server   = utf8mb4
collation-server       = utf8mb4_0900_ai_ci
max_connections        = 256

[client]
socket = ${SOCKET}
port   = ${PORT}
CNF

# --- datadir ----------------------------------------------------------------
if [ ! -d "${DATADIR}/mysql" ]; then
  echo "== initialising datadir at ${DATADIR}"
  mkdir -p "${DATADIR}"
  "${MYSQLD}" --defaults-file="${CONFFILE}" --initialize-insecure \
    || { echo "initialisation failed; see ${LOGFILE}" >&2; tail -20 "${LOGFILE}" >&2; exit 1; }
else
  echo "== reusing existing datadir at ${DATADIR}"
fi

# --- start ------------------------------------------------------------------
if [ -S "${SOCKET}" ] && "${MYSQL}" --defaults-file="${CONFFILE}" -uroot -e "SELECT 1" >/dev/null 2>&1; then
  echo "== already running on ${SOCKET}"
else
  echo "== starting mysqld on port ${PORT}"
  nohup "${MYSQLD}" --defaults-file="${CONFFILE}" >/dev/null 2>&1 &
  for _ in $(seq 1 60); do
    "${MYSQL}" --defaults-file="${CONFFILE}" -uroot -e "SELECT 1" >/dev/null 2>&1 && break
    sleep 1
  done
fi
"${MYSQL}" --defaults-file="${CONFFILE}" -uroot -e "SELECT VERSION()" \
  || { echo "mysqld did not come up; see ${LOGFILE}" >&2; tail -20 "${LOGFILE}" >&2; exit 1; }

# --- database and account ---------------------------------------------------
echo "== creating database ${DB} and user ${USER_NAME}"
"${MYSQL}" --defaults-file="${CONFFILE}" -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '${USER_NAME}'@'localhost' IDENTIFIED BY '${PASSWORD}';
CREATE USER IF NOT EXISTS '${USER_NAME}'@'127.0.0.1' IDENTIFIED BY '${PASSWORD}';
ALTER USER '${USER_NAME}'@'localhost' IDENTIFIED BY '${PASSWORD}';
ALTER USER '${USER_NAME}'@'127.0.0.1' IDENTIFIED BY '${PASSWORD}';
GRANT ALL PRIVILEGES ON \`${DB}\`.* TO '${USER_NAME}'@'localhost';
GRANT ALL PRIVILEGES ON \`${DB}\`.* TO '${USER_NAME}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

cat <<DONE

== ready
   port    ${PORT}
   socket  ${SOCKET}
   config  ${CONFFILE}
   log     ${LOGFILE}
   stop    kill \$(cat ${PIDFILE})

Put this in your task config's "mysql" section:

  "mysql": {
    "host": "127.0.0.1",
    "port": ${PORT},
    "user": "${USER_NAME}",
    "password": "\${FV_MYSQL_PASSWORD}",
    "database": "${DB}",
    "on_connect_error": "fail"
  }

MySQL 8.4 authenticates with caching_sha2_password. PyMySQL handles that on
its own for a local connection (verified against 8.4.2 with a cold auth
cache), so plain PyMySQL is enough:

  pip install "PyMySQL>=1.1,<2"

Add 'cryptography' only if the server requires TLS or lives on another host.
DONE
