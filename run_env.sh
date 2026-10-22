#!/usr/bin/env bash
# Start a local Db2 11.5 (Docker, needs --privileged) and wait for it to be ready.
set -e
docker run -d --name db2 --privileged=true -p 50000:50000 \
  -e LICENSE=accept -e DB2INST1_PASSWORD=pass1234 -e DBNAME=testdb \
  -e AUTOCONFIG=false -e ARCHIVE_LOGS=false ibmcom/db2:11.5.8.0
until docker logs db2 2>&1 | grep -q "Setup has completed"; do sleep 10; done
echo "Db2 ready on localhost:50000 (db2inst1 / pass1234, db testdb)"
