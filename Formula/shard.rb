class Shard < Formula
  include Language::Python::Virtualenv

  desc "TDD-driven, parallelized AI coding orchestrator"
  homepage "https://github.com/nihalgunu/Shard"
  url "https://github.com/nihalgunu/Shard/archive/refs/tags/v1.0.0rc1.tar.gz"
  sha256 "PLACEHOLDER"  # Update with actual sha256 after release
  license "Apache-2.0"

  depends_on "python@3.11"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/shard", "--version"
  end
end
