-- Success diagnostics were temporarily stored in error_text.  Remove them so
-- the repository detail page does not render successful uploads as errors.
UPDATE iac_scan_repositories
SET error_text = NULLIF(
    btrim(
        regexp_replace(
            regexp_replace(
                error_text,
                '(^|; )ASOC: repository created by ScanOps IaC scan(?=; |$)',
                '\\1',
                'g'
            ),
            '(^|; )ASOC: карточка найдена по URL VCS-привязки, slug ''[^'']+''(?=; |$)',
            '\\1',
            'g'
        ),
        ' ;'
    ),
    ''
)
WHERE error_text LIKE '%ASOC: repository created by ScanOps IaC scan%'
   OR error_text LIKE '%ASOC: карточка найдена по URL VCS-привязки%';
