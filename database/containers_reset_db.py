#!/usr/bin/env python
# -*- coding: utf-8 -*-

# "Standard library imports"
import json
import random
import subprocess
import sys

# "Imports from third party packages"
import MySQLdb
import MySQLdb.cursors

# "Imports from current project"
from settings import MYSQL_DATABASE_DB, MYSQL_DATABASE_PASSWORD, MYSQL_DATABASE_USER


def insert_config_values(config):
    db_obj = MySQLdb.connect(user=MYSQL_DATABASE_USER, passwd=MYSQL_DATABASE_PASSWORD,
                             db=MYSQL_DATABASE_DB,
                             cursorclass=MySQLdb.cursors.DictCursor)

    cursor = db_obj.cursor()

    # Very first, create the game. Copied from vm_reset_db.py
    new_game_id = random.randint(0, 1000000)
    cursor.execute("""INSERT INTO game (id) VALUES (%s)""",
                   (new_game_id,))

    # Insert team info from config
    print "Inserting team info into database"
    query = """INSERT INTO teams (team_name, services_ports_low, services_ports_high)
            VALUES (%s, %s, %s)"""
    for team in config["teams"]:
        values = (team["name"], team["services_ports_low"],
                  team["services_ports_high"])
        cursor.execute(query, values)
        team["id"] = db_obj.insert_id()

    db_obj.commit()
    print "done"

    return


def run_command_with_shell(cmd):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=True)
    stdout, stderr = process.communicate()
    retcode = process.returncode
    return (stdout, stderr, retcode)


def recreate_database():
    print "Recreating tables in DB"
    command = "mysql -u " + MYSQL_DATABASE_USER + " -p" + MYSQL_DATABASE_PASSWORD + " " + \
              MYSQL_DATABASE_DB + "< database.sql"
    out, err, ret = run_command_with_shell(command)
    if ret != 0:
        print "DB recreate failed with exitcode %d." % (ret)
        print "Stdout: ", out
        print "Stderr: ", err
        sys.exit(1)

    print "done"
    return


def main():
    if len(sys.argv) != 2:
        print "Usage: %s CONFIG_FILE" % (sys.argv[0])
        sys.exit(0)

    fh = open(sys.argv[1])
    config = json.load(fh)
    fh.close()
    recreate_database()
    insert_config_values(config)


if __name__ == "__main__":
    main()