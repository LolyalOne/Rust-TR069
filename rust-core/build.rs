fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=proto/usp.proto");

    let proto_files = ["proto/usp.proto"];
    let proto_includes = ["proto"];

    let mut config = prost_build::Config::new();
    config.compile_protos(&proto_files, &proto_includes)?;

    Ok(())
}
