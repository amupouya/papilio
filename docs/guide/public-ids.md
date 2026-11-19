# PostgreSQL public IDs in Papilio 3.0

This historical capability was removed in Papilio 4.0. Use the independent
[PermutedBigIntField](permuted-fields.md) on the column you choose, with ordinary
models, repositories and outputs. Existing databases require an explicit
[migration preserving issued IDs and foreign keys](../releases/4.0.0.md).
