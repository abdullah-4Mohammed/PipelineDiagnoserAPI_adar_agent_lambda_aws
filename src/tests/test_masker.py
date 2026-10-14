from shared import mask


def test_masks_sensitive_values_and_keeps_structure():
    log = ("arn:aws:iam::123456789012:role/x from 10.0.1.5 key AKIAABCDEFGHIJKLMNOP "
           "password=Sup3rS3cret! Bearer abcdefghijklmnop mail a.b@x.com again 123456789012")
    masked, counts = mask(log)
    for secret in ["123456789012", "10.0.1.5", "AKIAABCDEFGHIJKLMNOP", "Sup3rS3cret!", "abcdefghijklmnop", "a.b@x.com"]:
        assert secret not in masked
    assert "arn:aws:iam::ACCOUNT_1:role/x" in masked
    assert masked.count("ACCOUNT_1") == 2          # same value -> same placeholder
    assert "password=SECRET_1" in masked           # label stays readable
    assert counts == {"ACCOUNT": 1, "IP": 1, "AWS_KEY": 1, "SECRET": 1, "BEARER": 1, "EMAIL": 1}


def test_leaves_versions_and_normal_text_alone():
    masked, counts = mask("terraform 1.11.4, kubernetes v1.33, exit code 1")
    assert masked == "terraform 1.11.4, kubernetes v1.33, exit code 1"
    assert counts == {}
