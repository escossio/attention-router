CREATE TABLE evidence (
 id uuid PRIMARY KEY, excerpt text NOT NULL, origin text NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE information (
 id uuid PRIMARY KEY, subject text NOT NULL, attribute text NOT NULL,
 current_revision integer NOT NULL CHECK(current_revision > 0),
 UNIQUE(subject, attribute)
);
CREATE TABLE revision (
 information_id uuid NOT NULL REFERENCES information(id),
 number integer NOT NULL CHECK(number > 0), value text NOT NULL,
 evidence_id uuid NOT NULL REFERENCES evidence(id),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(information_id, number)
);
ALTER TABLE information ADD CONSTRAINT current_revision_fk
 FOREIGN KEY(id,current_revision) REFERENCES revision(information_id,number)
 DEFERRABLE INITIALLY DEFERRED;
GRANT CONNECT ON DATABASE memory_lab TO lab_reader, lab_writer;
GRANT USAGE ON SCHEMA public TO lab_reader, lab_writer;
GRANT SELECT ON evidence,information,revision TO lab_reader,lab_writer;
GRANT INSERT ON evidence,information,revision TO lab_writer;
GRANT UPDATE(current_revision) ON information TO lab_writer;
