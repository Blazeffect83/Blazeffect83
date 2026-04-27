import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const DB_PATH = process.env.VOLTFORGE_DB_PATH ?? "./voltforge.db";

let db: Database.Database | null = null;

export function getDb(): Database.Database {
  if (db) return db;
  mkdirSync(dirname(DB_PATH), { recursive: true });
  const handle = new Database(DB_PATH);
  handle.pragma("journal_mode = WAL");
  handle.pragma("synchronous = NORMAL");
  handle.pragma("foreign_keys = ON");

  handle.exec(`
    CREATE TABLE IF NOT EXISTS state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS photos (
      id TEXT PRIMARY KEY,
      created_at INTEGER NOT NULL,
      mime TEXT NOT NULL,
      bytes BLOB NOT NULL
    );
  `);

  db = handle;
  return handle;
}

export function readState<T>(key: string, fallback: T): T {
  const row = getDb()
    .prepare("SELECT value FROM state WHERE key = ?")
    .get(key) as { value: string } | undefined;
  if (!row) return fallback;
  try {
    return JSON.parse(row.value) as T;
  } catch {
    return fallback;
  }
}

export function writeState<T>(key: string, value: T): void {
  const stmt = getDb().prepare(
    `INSERT INTO state (key, value, updated_at)
     VALUES (?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  );
  stmt.run(key, JSON.stringify(value), Date.now());
}

export function savePhoto(id: string, mime: string, buf: Buffer): void {
  getDb()
    .prepare("INSERT OR REPLACE INTO photos (id, created_at, mime, bytes) VALUES (?, ?, ?, ?)")
    .run(id, Date.now(), mime, buf);
}

export function readPhoto(id: string): { mime: string; bytes: Buffer } | null {
  const row = getDb().prepare("SELECT mime, bytes FROM photos WHERE id = ?").get(id) as
    | { mime: string; bytes: Buffer }
    | undefined;
  return row ?? null;
}

export function deletePhoto(id: string): void {
  getDb().prepare("DELETE FROM photos WHERE id = ?").run(id);
}
