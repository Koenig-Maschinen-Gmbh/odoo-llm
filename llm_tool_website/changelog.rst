18.0.1.0.1 (2026-07-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Manifest ``description`` simplified to a plain-text paragraph (removed
  the indented ``•``-bullet RST block) so Odoo no longer emits Docutils
  "Unexpected indentation" / "Block quote ends without a blank line" warnings
  at module load.
