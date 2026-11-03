# encoding: ascii-8bit

# Create the overall gemspec
Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-flowautomator'
  s.summary = 'Flow Automator: graphical automation flows for OpenC3 COSMOS'
  s.description = <<-EOF
    Build automation flows by wiring nodes together: telemetry, schedule,
    packet and limits triggers, logic, commands, scripts and waits. A Python
    microservice keeps flows armed and runs them server side.
  EOF
  s.licenses = 'MIT'
  s.authors = ['Anonymous']
  s.email = ['name@domain.com']
  s.homepage = 'https://github.com/OpenC3/cosmos'
  s.platform = Gem::Platform::RUBY
  s.required_ruby_version = '>= 3.0'

  if ENV['VERSION']
    s.version = ENV['VERSION'].dup
  else
    time = Time.now.strftime("%Y%m%d%H%M%S")
    s.version = '0.0.0' + ".#{time}"
  end
  s.files = Dir.glob("{targets,lib,public,tools,microservices}/**/*").reject { |f| f.include?('__pycache__') } +
            %w(Rakefile README.md LICENSE.md plugin.txt)

  s.metadata = {
    # These fields are used when you submit your plugin to the OpenC3 Store at store.openc3.com
    # See this help page for more detail: https://store.openc3.com/help/guidelines
    "source_code_uri" => "https://github.com/your-github/plugin-repo",
    "openc3_store_title" => "Flow Automator",
    "openc3_store_description" => "Graphical automation flows: triggers, logic, commands, scripts and waits.",
    "openc3_store_keywords" => "automation, flow, triggers, workflow",
    "openc3_store_image" => "public/store_img.png",
    "openc3_cosmos_minimum_version" => "6.0.0", # OPTIONAL
    "openc3_store_access_type" => "public" # OPTIONAL
  }
end
