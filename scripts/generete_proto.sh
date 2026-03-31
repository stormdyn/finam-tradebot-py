# scripts/generate_proto.sh
python -m grpc_tools.protoc \
  -I vendor/finam-trade-api/proto \
  --python_out=src/api/grpc_stubs \
  --grpc_python_out=src/api/grpc_stubs \
  vendor/finam-trade-api/proto/grpc/tradeapi/v1/**/*.proto